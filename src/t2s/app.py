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

from t2s.engines import (
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
from t2s.pure import (
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
    build_say_command,
    cache_key,
    decode_chunk,
    handle_engine_event,
    handle_key,
    resolve_cache_directory,
    resolve_play_binary,
    resolve_say_binary,
    wrap_offsets,
)
from t2s.synthesis import (
    SynthesisFailed,
    SynthesisWorker,
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
class Arguments:
    file: str
    voice: str | None
    rate: int | None
    width: int
    start: int
    split_long: int | None
    ahead: int
    cache_directory: str | None
    cache_limit_megabytes: float
    say_binary: str | None
    play_binary: str | None
    gap_milliseconds: int
    data_format: str | None
    player: str


def arguments_from_namespace(namespace_arguments: argparse.Namespace) -> Arguments:
    return Arguments(
        file=namespace_arguments.file,
        voice=namespace_arguments.voice,
        rate=namespace_arguments.rate,
        width=namespace_arguments.width,
        start=namespace_arguments.start,
        split_long=namespace_arguments.split_long,
        ahead=namespace_arguments.ahead,
        cache_directory=namespace_arguments.cache_directory,
        cache_limit_megabytes=namespace_arguments.cache_limit_megabytes,
        say_binary=namespace_arguments.say_binary,
        play_binary=namespace_arguments.play_binary,
        gap_milliseconds=namespace_arguments.gap,
        data_format=namespace_arguments.data_format,
        player=namespace_arguments.player,
    )


@dataclass(frozen=True)
class Config:
    paragraphs: tuple[str, ...]
    start_index: int
    width: int
    voice: str | None
    rate: int | None
    say_binary: str | None
    play_binary: str | None
    cache_directory: str | None
    cache_limit_megabytes: float
    ahead: int
    player: str
    gap_milliseconds: int
    data_format: str | None


def config_from_arguments(arguments: Arguments, paragraphs: tuple[str, ...]) -> Config:
    return Config(
        paragraphs=paragraphs,
        start_index=arguments.start - 1,
        width=arguments.width,
        voice=arguments.voice,
        rate=arguments.rate,
        say_binary=arguments.say_binary,
        play_binary=arguments.play_binary,
        cache_directory=arguments.cache_directory,
        cache_limit_megabytes=arguments.cache_limit_megabytes,
        ahead=arguments.ahead,
        player=arguments.player,
        gap_milliseconds=arguments.gap_milliseconds,
        data_format=arguments.data_format,
    )


@dataclass
class KeyCell:
    pending: bytes


@dataclass(frozen=True)
class KeySource:
    file_descriptor: int
    restore: tuple[int | list[bytes | int], ...]
    close_file_descriptor: bool
    cell: KeyCell


def open_key_source() -> KeySource | None:
    if sys.stdin.isatty():
        file_descriptor = sys.stdin.fileno()
        close_file_descriptor = False
    else:
        try:
            file_descriptor = os.open("/dev/tty", os.O_RDONLY)
        except OSError:
            return None
        close_file_descriptor = True
    try:
        restore = tuple(termios.tcgetattr(file_descriptor))
        tty.setcbreak(file_descriptor)
    except termios.error:
        if close_file_descriptor:
            os.close(file_descriptor)
        return None
    return KeySource(
        file_descriptor=file_descriptor,
        restore=restore,
        close_file_descriptor=close_file_descriptor,
        cell=KeyCell(pending=b""),
    )


def open_app(config: Config) -> App:
    environment = os.environ
    say_binary = resolve_say_binary(config.say_binary, environment)
    data_format = config.data_format or default_data_format()
    say_command = build_say_command(say_binary, config.voice, config.rate, data_format)
    cache_keys = tuple(
        cache_key(paragraph, config.voice, config.rate, data_format)
        for paragraph in config.paragraphs
    )
    cache_directory = resolve_cache_directory(config.cache_directory, Path.home())
    cache_directory.mkdir(parents=True, exist_ok=True)
    prune_cache(cache_directory, config.cache_limit_megabytes)
    synthesis_worker = make_worker(
        config.paragraphs, cache_keys, cache_directory, say_command, ahead=config.ahead
    )
    play_command = (resolve_play_binary(config.play_binary, environment),)
    engine = make_engine(
        config.player,
        play_command,
        config.gap_milliseconds,
        environment,
        audio=load_audio_library(),
    )
    return App(
        paragraphs=config.paragraphs,
        start_index=config.start_index,
        width=config.width,
        use_ansi=sys.stdout.isatty(),
        key_source=open_key_source(),
        synthesis_worker=synthesis_worker,
        engine=engine,
    )


@dataclass(frozen=True)
class App:
    paragraphs: tuple[str, ...]
    start_index: int
    width: int
    use_ansi: bool
    key_source: KeySource | None
    synthesis_worker: SynthesisWorker
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
            clear_failure(app.synthesis_worker, index)
        case SyncTo(index):
            sync_to(app, index)
        case Note(text):
            emit_note(app, text)
        case Warn(text):
            emit_warn(app, text)
    return state, ()


def try_play(app: App, index: int, note_text: str | None = None) -> PlayOutcome:
    engine_stop_stream(app.engine)
    set_cursor(app.synthesis_worker, index)
    show_paragraph(app, index, note_text)
    match ensure(app.synthesis_worker, index):
        case SynthesisFailed(detail=detail):
            message = f"! could not render paragraph: {detail}"
            if app.key_source is not None:
                emit_warn(app, message)
                emit_note(app, "space: retry, n/p: paragraph, q: quit")
                return "paused"
            emit_warn(app, message + " - continuing with next paragraph")
            return "failed"
        case path:
            engine_play(app.engine, index, path)
            prime_next(app, index)
            return "playing"


def prime_next(app: App, index: int) -> None:
    next_index = index + 1
    if next_index < len(app.paragraphs):
        path = path_for(app.synthesis_worker, next_index)
        if path.exists():
            engine_prime(app.engine, next_index, path)


def sync_to(app: App, index: int) -> None:
    set_cursor(app.synthesis_worker, index)
    show_paragraph(app, index)
    prime_next(app, index)


def run(app: App) -> int:
    state = AppState(
        index=app.start_index,
        paragraph_count=len(app.paragraphs),
        interactive=app.key_source is not None,
    )
    start_worker(app.synthesis_worker)
    code = 0
    try:
        state = interpret(app, state, (TryPlay(state.index),))
        while state.running:
            state = tick(app, state)
        code = 1 if state.had_errors else 0
    except KeyboardInterrupt:
        emit_note(app, "interrupted")
        code = 130
    finally:
        restore_terminal(app)
        engine_stop_stream(app.engine)
        engine_close(app.engine)
        stop_worker(app.synthesis_worker)
        if app.use_ansi:
            sys.stdout.write("\x1b[0m\x1b[?25h")
            sys.stdout.flush()
    return code


def restore_terminal(app: App) -> None:
    key_source = app.key_source
    if key_source is None:
        return
    with contextlib.suppress(termios.error):
        termios.tcsetattr(
            key_source.file_descriptor, termios.TCSADRAIN, list(key_source.restore)
        )
    if key_source.close_file_descriptor:
        with contextlib.suppress(OSError):
            os.close(key_source.file_descriptor)


def tick(app: App, state: AppState) -> AppState:
    return drain_events(app, poll_keys(app, state))


def poll_keys(app: App, state: AppState) -> AppState:
    key_source = app.key_source
    file_descriptors: list[int] = (
        [key_source.file_descriptor] if key_source is not None else []
    )
    try:
        ready, _, _ = select.select(file_descriptors, [], [], 0.05)
    except OSError, ValueError:
        ready = []
    if key_source is not None and key_source.file_descriptor in ready:
        try:
            data = os.read(key_source.file_descriptor, 256)
        except OSError:
            data = b""
        text, key_source.cell.pending = decode_chunk(key_source.cell.pending, data)
        for key in text:
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
    header = f"- {index + 1}/{len(app.paragraphs)}"
    if note_text:
        header += f" {note_text}"
    header += " -"
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
