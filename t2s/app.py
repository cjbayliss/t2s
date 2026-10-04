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
    ChainEngine,
    SubprocessEngine,
    default_data_format,
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
from .synth import SynthesisError, SynthWorker, prune_cache

DIM = "\x1b[2m"
RESET = "\x1b[0m"
SHOW_CURSOR = "\x1b[0m\x1b[?25h"


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
    restore: list[int | list[bytes | int]]
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
        restore = termios.tcgetattr(fd)
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
    synth_worker = SynthWorker(
        config.paragraphs, cache_keys, cache_dir, say_cmd, ahead=config.ahead
    )
    play_cmd = (resolve_play_bin(config.play_bin, env),)
    engine = make_engine(config.player, play_cmd, config.gap_ms, env)
    return App(
        paragraphs=config.paragraphs,
        start_index=config.start_index,
        width=config.width,
        use_ansi=sys.stdout.isatty(),
        key_source=open_key_source(),
        synth_worker=synth_worker,
        engine=engine,
    )


class App:
    def __init__(
        self,
        *,
        paragraphs: tuple[str, ...],
        start_index: int,
        width: int,
        use_ansi: bool,
        key_source: KeySource | None,
        synth_worker: SynthWorker,
        engine: ChainEngine | SubprocessEngine,
    ) -> None:
        self.paragraphs = paragraphs
        self.start_index = start_index
        self.width = width
        self.use_ansi = use_ansi
        self.key_source = key_source
        self.synth_worker = synth_worker
        self.engine = engine

    def _interpret(self, state: AppState, effects: Effects) -> AppState:
        pending: deque[Effect] = deque(effects)
        while pending:
            state, produced = self._perform(state, pending.popleft())
            pending.extend(produced)
        return state

    def _perform(self, state: AppState, effect: Effect) -> tuple[AppState, Effects]:
        match effect:
            case TryPlay(index, note):
                return apply_play_outcome(state, index, self._try_play(index, note))
            case StopStream():
                self.engine.stop_stream()
            case ClearFailure(index):
                self.synth_worker.clear_failure(index)
            case SyncTo(index):
                self._sync_to(index)
            case Note(text):
                self._note(text)
            case Warn(text):
                self._warn(text)
        return state, ()

    def _try_play(self, index: int, note: str | None = None) -> PlayOutcome:
        self.engine.stop_stream()
        self.synth_worker.set_cursor(index)
        self.show_paragraph(index, note)
        try:
            path = self.synth_worker.ensure(index)
        except SynthesisError as exc:
            msg = f"! could not render paragraph: {exc.detail}"
            if self.key_source is not None:
                self._warn(msg)
                self._note("space: retry · n/p: paragraph · q: quit")
                return "paused"
            self._warn(msg + " — continuing with next paragraph")
            return "failed"
        self.engine.play(index, path)
        self._prime_next(index)
        return "playing"

    def _prime_next(self, index: int) -> None:
        next_index = index + 1
        if next_index < len(self.paragraphs):
            path = self.synth_worker.path_for(next_index)
            if path.exists():
                self.engine.prime(next_index, path)

    def _sync_to(self, index: int) -> None:
        self.synth_worker.set_cursor(index)
        self.show_paragraph(index)
        self._prime_next(index)

    def run(self) -> int:
        state = AppState(
            index=self.start_index,
            n_paragraphs=len(self.paragraphs),
            interactive=self.key_source is not None,
        )
        self.synth_worker.start()
        try:
            state = self._interpret(state, (TryPlay(state.index),))
            while state.running:
                state = self._tick(state)
        except KeyboardInterrupt:
            self._note("· interrupted")
        finally:
            self._restore_terminal()
            self.engine.stop_stream()
            self.engine.close()
            self.synth_worker.stop()
            if self.use_ansi:
                sys.stdout.write(SHOW_CURSOR)
                sys.stdout.flush()
        return 0

    def _restore_terminal(self) -> None:
        key_source = self.key_source
        if key_source is None:
            return
        with contextlib.suppress(termios.error):
            termios.tcsetattr(key_source.fd, termios.TCSADRAIN, key_source.restore)
        if key_source.close_fd:
            with contextlib.suppress(OSError):
                os.close(key_source.fd)

    def _tick(self, state: AppState) -> AppState:
        return self._drain_events(self._poll_keys(state))

    def _poll_keys(self, state: AppState) -> AppState:
        key_source = self.key_source
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
                    state, key, self.engine.current_index()
                )
                state = self._interpret(next_state, effects)
        return state

    def _drain_events(self, state: AppState) -> AppState:
        while True:
            try:
                event = self.engine.events.get_nowait()
            except queue.Empty:
                return state
            next_state, effects = handle_engine_event(state, event)
            state = self._interpret(next_state, effects)

    def show_paragraph(self, index: int, note: str | None = None) -> None:
        header = f"── ¶ {index + 1}/{len(self.paragraphs)}"
        if note:
            header += f" {note}"
        header += " ──"
        print(self._dim(header))
        for line, _ in wrap_offsets(self.paragraphs[index], self.width):
            print(line)
        sys.stdout.flush()

    def _dim(self, text: str) -> str:
        return f"{DIM}{text}{RESET}" if self.use_ansi else text

    def _note(self, text: str) -> None:
        print(self._dim(text))
        sys.stdout.flush()

    def _warn(self, text: str) -> None:
        print(text, file=sys.stderr)
        sys.stderr.flush()
