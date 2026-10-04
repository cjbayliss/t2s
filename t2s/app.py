from __future__ import annotations

import argparse
import contextlib
import os
import queue
import select
import sys
import termios
import tty
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .engines import (
    Engine,
    default_data_format,
    engine_close,
    engine_current_index,
    engine_play,
    engine_prime,
    engine_stop_stream,
    load_audio_library,
    make_engine,
)
from .pure import (
    AppState,
    ClearFailure,
    Effect,
    Effects,
    Note,
    PlayOutcome,
    StopStream,
    SyncTo,
    TryPlay,
    Warn,
    apply_play_outcome,
    build_say_cmd,
    cache_key,
    handle_engine_event,
    handle_key,
    resolve_cache_dir,
    resolve_play_bin,
    resolve_say_bin,
    wrap_offsets,
)
from .synth import (
    SynthesisFailed,
    SynthWorker,
    clear_failure,
    ensure,
    make_worker,
    path_for,
    prune_cache,
    set_cursor,
    start_worker,
    stop_worker,
)


@dataclass(frozen=True)
class Args:
    file: str
    voice: str | None
    rate: int | None
    width: int
    start: int
    split_long: int | None
    ahead: int
    cache_dir: str | None
    cache_limit_mb: float
    say_bin: str | None
    play_bin: str | None
    gap_ms: int
    data_format: str | None
    player: str


def args_from_namespace(ns: argparse.Namespace) -> Args:
    return Args(
        file=ns.file,
        voice=ns.voice,
        rate=ns.rate,
        width=ns.width,
        start=ns.start,
        split_long=ns.split_long,
        ahead=ns.ahead,
        cache_dir=ns.cache_dir,
        cache_limit_mb=ns.cache_limit_mb,
        say_bin=ns.say_bin,
        play_bin=ns.play_bin,
        gap_ms=ns.gap,
        data_format=ns.data_format,
        player=ns.player,
    )


@dataclass(frozen=True)
class Config:
    paragraphs: tuple[str, ...]
    start_index: int
    width: int
    voice: str | None
    rate: int | None
    say_bin: str | None
    play_bin: str | None
    cache_dir: str | None
    cache_limit_mb: float
    ahead: int
    player: str
    gap_ms: int
    data_format: str | None


def config_from_args(args: Args, paragraphs: tuple[str, ...]) -> Config:
    return Config(
        paragraphs=paragraphs,
        start_index=args.start - 1,
        width=args.width,
        voice=args.voice,
        rate=args.rate,
        say_bin=args.say_bin,
        play_bin=args.play_bin,
        cache_dir=args.cache_dir,
        cache_limit_mb=args.cache_limit_mb,
        ahead=args.ahead,
        player=args.player,
        gap_ms=args.gap_ms,
        data_format=args.data_format,
    )


@dataclass(frozen=True)
class KeySource:
    fd: int
    restore: tuple[int | list[bytes | int], ...]
    close_fd: bool


def open_key_source() -> KeySource | None:
    if sys.stdin.isatty():
        fd = sys.stdin.fileno()
        close_fd = False
    else:
        try:
            fd = os.open("/dev/tty", os.O_RDONLY)
        except OSError:
            return None
        close_fd = True
    try:
        restore = tuple(termios.tcgetattr(fd))
        tty.setcbreak(fd)
    except termios.error:
        if close_fd:
            os.close(fd)
        return None
    return KeySource(fd=fd, restore=restore, close_fd=close_fd)


def open_app(config: Config) -> App:
    env = os.environ
    say_bin = resolve_say_bin(config.say_bin, env)
    data_format = config.data_format or default_data_format()
    say_cmd = build_say_cmd(say_bin, config.voice, config.rate, data_format)
    cache_keys = tuple(
        cache_key(paragraph, config.voice, config.rate, data_format)
        for paragraph in config.paragraphs
    )
    cache_dir = resolve_cache_dir(config.cache_dir, Path.home())
    cache_dir.mkdir(parents=True, exist_ok=True)
    prune_cache(cache_dir, config.cache_limit_mb)
    synth_worker = make_worker(
        config.paragraphs, cache_keys, cache_dir, say_cmd, ahead=config.ahead
    )
    play_cmd = (resolve_play_bin(config.play_bin, env),)
    engine = make_engine(
        config.player, play_cmd, config.gap_ms, env, audio=load_audio_library()
    )
    return App(
        paragraphs=config.paragraphs,
        start_index=config.start_index,
        width=config.width,
        use_ansi=sys.stdout.isatty(),
        key_source=open_key_source(),
        synth_worker=synth_worker,
        engine=engine,
    )


@dataclass(frozen=True)
class App:
    paragraphs: tuple[str, ...]
    start_index: int
    width: int
    use_ansi: bool
    key_source: KeySource | None
    synth_worker: SynthWorker
    engine: Engine


def interpret(app: App, state: AppState, effects: Effects) -> AppState:
    pending: deque[Effect] = deque(effects)
    while pending:
        state, produced = perform(app, state, pending.popleft())
        pending.extend(produced)
    return state


def perform(app: App, state: AppState, effect: Effect) -> tuple[AppState, Effects]:
    match effect:
        case TryPlay(index, note):
            return apply_play_outcome(state, index, try_play(app, index, note))
        case StopStream():
            engine_stop_stream(app.engine)
        case ClearFailure(index):
            clear_failure(app.synth_worker, index)
        case SyncTo(index):
            sync_to(app, index)
        case Note(text):
            emit_note(app, text)
        case Warn(text):
            emit_warn(app, text)
    return state, ()


def try_play(app: App, index: int, note_text: str | None = None) -> PlayOutcome:
    engine_stop_stream(app.engine)
    set_cursor(app.synth_worker, index)
    show_paragraph(app, index, note_text)
    match ensure(app.synth_worker, index):
        case SynthesisFailed(detail=detail):
            msg = f"! could not render paragraph: {detail}"
            if app.key_source is not None:
                emit_warn(app, msg)
                emit_note(app, "space: retry · n/p: paragraph · q: quit")
                return "paused"
            emit_warn(app, msg + " — continuing with next paragraph")
            return "failed"
        case path:
            engine_play(app.engine, index, path)
            prime_next(app, index)
            return "playing"


def prime_next(app: App, index: int) -> None:
    next_index = index + 1
    if next_index < len(app.paragraphs):
        path = path_for(app.synth_worker, next_index)
        if path.exists():
            engine_prime(app.engine, next_index, path)


def sync_to(app: App, index: int) -> None:
    set_cursor(app.synth_worker, index)
    show_paragraph(app, index)
    prime_next(app, index)


def run(app: App) -> int:
    state = AppState(
        index=app.start_index,
        n_paragraphs=len(app.paragraphs),
        interactive=app.key_source is not None,
    )
    start_worker(app.synth_worker)
    try:
        state = interpret(app, state, (TryPlay(state.index),))
        while state.running:
            state = tick(app, state)
    except KeyboardInterrupt:
        emit_note(app, "· interrupted")
    finally:
        restore_terminal(app)
        engine_stop_stream(app.engine)
        engine_close(app.engine)
        stop_worker(app.synth_worker)
        if app.use_ansi:
            sys.stdout.write("\x1b[0m\x1b[?25h")
            sys.stdout.flush()
    return 0


def restore_terminal(app: App) -> None:
    key_source = app.key_source
    if key_source is None:
        return
    with contextlib.suppress(termios.error):
        termios.tcsetattr(key_source.fd, termios.TCSADRAIN, list(key_source.restore))
    if key_source.close_fd:
        with contextlib.suppress(OSError):
            os.close(key_source.fd)


def tick(app: App, state: AppState) -> AppState:
    return drain_events(app, poll_keys(app, state))


def poll_keys(app: App, state: AppState) -> AppState:
    key_source = app.key_source
    fds: list[int] = [key_source.fd] if key_source is not None else []
    try:
        ready, _, _ = select.select(fds, [], [], 0.05)
    except (OSError, ValueError):
        ready = []
    if key_source is not None and key_source.fd in ready:
        try:
            data = os.read(key_source.fd, 256)
        except OSError:
            data = b""
        for key in data.decode("utf-8", "ignore"):
            next_state, effects = handle_key(
                state, key, engine_current_index(app.engine)
            )
            state = interpret(app, next_state, effects)
    return state


def drain_events(app: App, state: AppState) -> AppState:
    while True:
        try:
            event = app.engine.events.get_nowait()
        except queue.Empty:
            return state
        next_state, effects = handle_engine_event(state, event)
        state = interpret(app, next_state, effects)


def show_paragraph(app: App, index: int, note_text: str | None = None) -> None:
    header = f"── ¶ {index + 1}/{len(app.paragraphs)}"
    if note_text:
        header += f" {note_text}"
    header += " ──"
    print(dim(app, header))
    for line, _ in wrap_offsets(app.paragraphs[index], app.width):
        print(line)
    sys.stdout.flush()


def dim(app: App, text: str) -> str:
    return f"\x1b[2m{text}\x1b[0m" if app.use_ansi else text


def emit_note(app: App, text: str) -> None:
    print(dim(app, text))
    sys.stdout.flush()


def emit_warn(app: App, text: str) -> None:
    print(text, file=sys.stderr)
    sys.stderr.flush()
