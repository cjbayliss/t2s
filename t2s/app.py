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
    build_say_cmd,
    cache_key,
    handle_engine_event,
    handle_key,
    play_resolved,
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
    gap: int
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
        gap=ns.gap,
        data_format=ns.data_format,
        player=ns.player,
    )


@dataclass(frozen=True)
class Config:
    paras: tuple[str, ...]
    start_idx: int
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


def config_from_args(args: Args, paras: tuple[str, ...]) -> Config:
    return Config(
        paras=paras,
        start_idx=args.start - 1,
        width=args.width,
        voice=args.voice,
        rate=args.rate,
        say_bin=args.say_bin,
        play_bin=args.play_bin,
        cache_dir=args.cache_dir,
        cache_limit_mb=args.cache_limit_mb,
        ahead=args.ahead,
        player=args.player,
        gap_ms=args.gap,
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
        owned = False
    else:
        try:
            fd = os.open("/dev/tty", os.O_RDONLY)
        except OSError:
            return None
        owned = True
    try:
        restore = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except termios.error:
        if owned:
            os.close(fd)
        return None
    return KeySource(fd=fd, restore=restore, close_fd=owned)


def open_app(config: Config) -> App:
    env = os.environ
    say_bin = resolve_say_bin(config.say_bin, env)
    fmt = config.data_format or default_data_format()
    say_cmd = build_say_cmd(say_bin, config.voice, config.rate, fmt)
    keys = tuple(cache_key(p, config.voice, config.rate, fmt) for p in config.paras)
    cache_dir = resolve_cache_dir(config.cache_dir, Path.home())
    cache_dir.mkdir(parents=True, exist_ok=True)
    prune_cache(cache_dir, config.cache_limit_mb)
    synth = SynthWorker(config.paras, keys, cache_dir, say_cmd, ahead=config.ahead)
    play_cmd = (resolve_play_bin(config.play_bin, env),)
    engine = make_engine(config.player, play_cmd, config.gap_ms, env)
    return App(
        paras=config.paras,
        start_idx=config.start_idx,
        width=config.width,
        ansi=sys.stdout.isatty(),
        key=open_key_source(),
        synth=synth,
        engine=engine,
    )


class App:
    def __init__(
        self,
        *,
        paras: tuple[str, ...],
        start_idx: int,
        width: int,
        ansi: bool,
        key: KeySource | None,
        synth: SynthWorker,
        engine: ChainEngine | SubprocessEngine,
    ) -> None:
        self.paras = paras
        self.start_idx = start_idx
        self.width = width
        self.ansi = ansi
        self.key = key
        self.synth = synth
        self.engine = engine

    def _interpret(self, s: AppState, effects: Effects) -> AppState:
        pending: deque[Effect] = deque(effects)
        while pending:
            s, produced = self._perform(s, pending.popleft())
            pending.extend(produced)
        return s

    def _perform(self, s: AppState, ef: Effect) -> tuple[AppState, Effects]:
        match ef:
            case TryPlay(idx, note):
                return play_resolved(s, idx, self._try_play(idx, note))
            case StopStream():
                self.engine.stop_stream()
            case ClearFailure(idx):
                self.synth.clear_failure(idx)
            case SyncTo(idx):
                self._sync_to(idx)
            case Note(text):
                self._note(text)
            case Warn(text):
                self._warn(text)
        return s, ()

    def _try_play(self, idx: int, note: str | None = None) -> PlayOutcome:
        self.engine.stop_stream()
        self.synth.set_cursor(idx)
        self.show_paragraph(idx, note)
        try:
            path = self.synth.ensure(idx)
        except SynthesisError as exc:
            msg = f"! could not render paragraph: {exc.detail}"
            if self.key is not None:
                self._warn(msg)
                self._note("space: retry · n/p: paragraph · q: quit")
                return "paused"
            self._warn(msg + " — continuing with next paragraph")
            return "failed"
        self.engine.play(idx, path)
        self._prime_next(idx)
        return "playing"

    def _prime_next(self, idx: int) -> None:
        nxt = idx + 1
        if nxt < len(self.paras):
            path = self.synth.path_for(nxt)
            if path.exists():
                self.engine.prime(nxt, path)

    def _sync_to(self, idx: int) -> None:
        self.synth.set_cursor(idx)
        self.show_paragraph(idx)
        self._prime_next(idx)

    def run(self) -> int:
        state = AppState(
            idx=self.start_idx,
            n_paras=len(self.paras),
            interactive=self.key is not None,
        )
        self.synth.start()
        try:
            state = self._interpret(state, (TryPlay(state.idx),))
            while state.running:
                state = self._tick(state)
        except KeyboardInterrupt:
            self._note("· interrupted")
        finally:
            self._restore_terminal()
            self.engine.stop_stream()
            self.engine.close()
            self.synth.stop()
            if self.ansi:
                sys.stdout.write(SHOW_CURSOR)
                sys.stdout.flush()
        return 0

    def _restore_terminal(self) -> None:
        key = self.key
        if key is None:
            return
        with contextlib.suppress(termios.error):
            termios.tcsetattr(key.fd, termios.TCSADRAIN, key.restore)
        if key.close_fd:
            with contextlib.suppress(OSError):
                os.close(key.fd)

    def _tick(self, s: AppState) -> AppState:
        return self._drain_events(self._poll_keys(s))

    def _poll_keys(self, s: AppState) -> AppState:
        key = self.key
        fds: list[int] = [key.fd] if key is not None else []
        try:
            ready, _, _ = select.select(fds, [], [], 0.05)
        except (OSError, ValueError):
            ready = []
        if key is not None and key.fd in ready:
            try:
                data = os.read(key.fd, 256)
            except OSError:
                data = b""
            for ch in data.decode("utf-8", "ignore"):
                s2, efs = handle_key(s, ch, self.engine.current_index())
                s = self._interpret(s2, efs)
        return s

    def _drain_events(self, s: AppState) -> AppState:
        while True:
            try:
                ev = self.engine.events.get_nowait()
            except queue.Empty:
                return s
            s2, efs = handle_engine_event(s, ev)
            s = self._interpret(s2, efs)

    def show_paragraph(self, idx: int, note: str | None = None) -> None:
        header = f"── ¶ {idx + 1}/{len(self.paras)}"
        if note:
            header += f" {note}"
        header += " ──"
        print(self._dim(header))
        for line, _ in wrap_offsets(self.paras[idx], self.width):
            print(line)
        sys.stdout.flush()

    def _dim(self, text: str) -> str:
        return f"{DIM}{text}{RESET}" if self.ansi else text

    def _note(self, text: str) -> None:
        print(self._dim(text))
        sys.stdout.flush()

    def _warn(self, text: str) -> None:
        print(text, file=sys.stderr)
        sys.stderr.flush()
