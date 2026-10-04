"""The application shell: pure transitions in, real effects out.

App owns the synthesis worker, the playback engine, the keyboard and the
display.  It holds no decisions of its own: keypresses and engine events
are folded through the pure transitions in `pure.py`, and the resulting
effect values are performed here, at the edge.  Construction effects
(device probing, cache pruning, terminal detection) live in open_app,
not in App itself.
"""

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
    wrap_offsets,
)
from .synth import SynthesisError, SynthWorker, prune_cache

DIM = "\x1b[2m"
RESET = "\x1b[0m"
SHOW_CURSOR = "\x1b[0m\x1b[?25h"


@dataclass(frozen=True)
class Config:
    """Everything one run needs, resolved and immutable."""

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


def config_from_args(args: argparse.Namespace, paras: tuple[str, ...]) -> Config:
    """Freeze parsed CLI options plus the paragraph list into a Config."""
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


def open_app(config: Config) -> App:
    """Resolve the configuration into live effectful components.

    This is the construction edge: every side effect of building the app
    (device probing, cache creation and pruning, terminal detection)
    happens here, so App itself is born with everything decided.
    """
    say_bin = config.say_bin or os.environ.get("T2S_SAY_BIN") or "say"
    fmt = config.data_format or default_data_format()
    say_cmd = build_say_cmd(say_bin, config.voice, config.rate, fmt)
    keys = tuple(cache_key(p, config.voice, config.rate, fmt) for p in config.paras)
    cache_dir = (
        Path(config.cache_dir)
        if config.cache_dir
        else (Path.home() / "Library" / "Caches" / "t2s")
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    prune_cache(cache_dir, config.cache_limit_mb)
    synth = SynthWorker(config.paras, keys, cache_dir, say_cmd, ahead=config.ahead)

    play_cmd = (config.play_bin or os.environ.get("T2S_PLAY_BIN") or "afplay",)
    engine = make_engine(config.player, play_cmd, config.gap_ms, os.environ)

    # Keyboard source: stdin when it is a terminal, otherwise the
    # controlling terminal (so `cat book.txt | t2s` stays interactive).
    # Without either, t2s runs non-interactively to the end.
    key_fd: int | None
    if sys.stdin.isatty():
        key_fd = sys.stdin.fileno()
    else:
        try:
            key_fd = os.open("/dev/tty", os.O_RDONLY)
        except OSError:
            key_fd = None

    return App(
        paras=config.paras,
        start_idx=config.start_idx,
        width=config.width,
        ansi=sys.stdout.isatty(),
        key_fd=key_fd,
        synth=synth,
        engine=engine,
    )


class App:
    """Effectful shell around the pure application state machine.

    The loop gathers keypresses and engine events, folds each through its
    pure transition, and interprets the resulting effects against the
    world (engine calls, synthesis, display).
    """

    def __init__(
        self,
        *,
        paras: tuple[str, ...],
        start_idx: int,
        width: int,
        ansi: bool,
        key_fd: int | None,
        synth: SynthWorker,
        engine: ChainEngine | SubprocessEngine,
    ) -> None:
        self.paras = paras
        self.start_idx = start_idx
        self.width = width
        self.ansi = ansi
        self.key_fd = key_fd
        self.synth = synth
        self.engine = engine
        self._termios_fd: int | None = None
        self._termios_old: list[int | list[bytes | int]] | None = None

    # -- interpreter ---------------------------------------------------------

    def _interpret(self, s: AppState, effects: Effects) -> AppState:
        """Perform effects FIFO-style, folding their feedback back in.

        TryPlay yields a play outcome that re-enters as play_resolved;
        because feedback goes through this work queue rather than
        recursion, a document where every render fails costs no stack
        depth.
        """
        pending: deque[Effect] = deque(effects)
        while pending:
            s, produced = self._perform(s, pending.popleft())
            pending.extend(produced)
        return s

    def _perform(self, s: AppState, ef: Effect) -> tuple[AppState, Effects]:
        """Perform one effect; return the feedback effects it produces."""
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

    # -- playback ------------------------------------------------------------

    def _try_play(self, idx: int, note: str | None = None) -> PlayOutcome:
        """Start playing paragraph idx: the effectful part of playing.

        Returns "playing", "paused" (interactive synthesis failure — wait
        for the user) or "failed" (synthesis failed; non-interactive mode
        should move on).  The state fold happens in play_resolved.
        """
        self.engine.stop_stream()
        self.synth.set_cursor(idx)
        self.show_paragraph(idx, note)
        try:
            path = self.synth.ensure(idx)
        except SynthesisError as exc:
            msg = f"! could not render paragraph: {exc.detail}"
            if self.key_fd is not None:
                self._warn(msg)
                self._note("space: retry · n/p: paragraph · q: quit")
                return "paused"
            self._warn(msg + " — continuing with next paragraph")
            return "failed"
        self.engine.play(idx, path)
        self._prime_next(idx)
        return "playing"

    def _prime_next(self, idx: int) -> None:
        """Decode the next paragraph ahead of time so the chain reaches it."""
        nxt = idx + 1
        if nxt < len(self.paras):
            path = self.synth.path_for(nxt)
            if path.exists():
                self.engine.prime(nxt, path)

    def _sync_to(self, idx: int) -> None:
        """The engine chained into paragraph idx on its own — catch up."""
        self.synth.set_cursor(idx)
        self.show_paragraph(idx)
        self._prime_next(idx)

    def run(self) -> int:
        if self.key_fd is not None:
            try:
                self._termios_old = termios.tcgetattr(self.key_fd)
                tty.setcbreak(self.key_fd)
                self._termios_fd = self.key_fd
            except termios.error:
                self._termios_old = None
                self._termios_fd = None
                self.key_fd = None
        state = AppState(
            idx=self.start_idx,
            n_paras=len(self.paras),
            interactive=self.key_fd is not None,
        )
        self.synth.start()
        try:
            state = self._interpret(state, (TryPlay(state.idx),))
            while state.running:
                state = self._tick(state)
        except KeyboardInterrupt:
            self._note("· interrupted")
        finally:
            if self._termios_fd is not None and self._termios_old is not None:
                with contextlib.suppress(termios.error):
                    termios.tcsetattr(
                        self._termios_fd, termios.TCSADRAIN, self._termios_old
                    )
            self.engine.stop_stream()
            self.engine.close()
            self.synth.stop()
            if self.ansi:
                sys.stdout.write(SHOW_CURSOR)
                sys.stdout.flush()
        return 0

    # -- event loop ----------------------------------------------------------

    def _tick(self, s: AppState) -> AppState:
        """One tick: read keypresses, then sync with the engine."""
        return self._drain_events(self._poll_keys(s))

    def _poll_keys(self, s: AppState) -> AppState:
        """Wait briefly for a keypress, then fold each key in turn."""
        fds: list[int] = [self.key_fd] if self.key_fd is not None else []
        try:
            ready, _, _ = select.select(fds, [], [], 0.05)
        except (OSError, ValueError):
            ready = []
        if self.key_fd is not None and self.key_fd in ready:
            try:
                data = os.read(self.key_fd, 256)
            except OSError:
                data = b""
            for ch in data.decode("utf-8", "ignore"):
                s2, efs = handle_key(s, ch, self.engine.current_index())
                s = self._interpret(s2, efs)
        return s

    def _drain_events(self, s: AppState) -> AppState:
        """Process engine events: failures, paragraph ends, chain syncs."""
        while True:
            try:
                ev = self.engine.events.get_nowait()
            except queue.Empty:
                return s
            s2, efs = handle_engine_event(s, ev)
            s = self._interpret(s2, efs)

    # -- display -------------------------------------------------------------

    def show_paragraph(self, idx: int, note: str | None = None) -> None:
        header = f"── ¶ {idx + 1}/{len(self.paras)}"
        if note:
            header += f" {note}"
        header += " ──"
        print(self._dim(header))
        for line, _ in wrap_offsets(self.paras[idx], self.width):
            print(line)
        sys.stdout.flush()

    # -- output helpers ------------------------------------------------------

    def _dim(self, text: str) -> str:
        return f"{DIM}{text}{RESET}" if self.ansi else text

    def _note(self, text: str) -> None:
        print(self._dim(text))
        sys.stdout.flush()

    def _warn(self, text: str) -> None:
        print(text, file=sys.stderr)
        sys.stderr.flush()
