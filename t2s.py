#!/usr/bin/env python3
"""t2s — read a document aloud with macOS say(1), one paragraph at a time.

Paragraphs are synthesized offline to cached audio files a few ahead of
playback and streamed through a single long-lived audio device, chaining
straight from one paragraph's samples into the next with no gap.  That
removes the synthesis delay between paragraphs, makes pause/restart
instant, and keeps synthesis away from the audio device entirely:

1. `say` ignores the terminal width when printing interactively; t2s prints
   each paragraph wrapped to a fixed width (default 72 columns).
2. `say` cannot pause.  In t2s, <space> stops the current paragraph and
   <space> again replays it from its beginning — from cache, instantly.
3. `say` dies without warning when an audio device appears or disappears.
   Here only the playback engine touches the audio device, so a device
   change can at worst interrupt the current paragraph: t2s reports it and
   waits; <space> replays the cached file.  Your place is never lost
   beyond the current paragraph.
4. `say` cannot start mid-document.  t2s numbers paragraphs and `--start N`
   begins at paragraph N (1-based).

Usage:
    t2s chapter.txt
    cat chapter.txt | t2s --voice Fred -r 190
    t2s chapter.txt --start 14 --split-long 1200

Keys (when run from a terminal):
    space   stop / replay the current paragraph
    n / p   next / previous paragraph
    q       quit (Ctrl-C works too)
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import queue
import re
import select
import subprocess
import sys
import termios
import threading
import time
import tty
import wave
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import reduce
from itertools import accumulate, chain
from operator import attrgetter
from pathlib import Path
from typing import Any, Literal, cast

try:
    import miniaudio

    HAVE_MINIAUDIO = True
except ImportError:  # pragma: no cover - environment dependent
    miniaudio = None
    HAVE_MINIAUDIO = False

__version__ = "0.4.0"

# --------------------------------------------------------------------------- #
# Terminal escapes / stream format                                            #
# --------------------------------------------------------------------------- #

DIM = "\x1b[2m"
RESET = "\x1b[0m"
SHOW_CURSOR = "\x1b[0m\x1b[?25h"

# Synthesis is pinned to one format per session so every cached file matches
# the single audio device opened for the whole session.  The default rate is
# the output device's native rate: resampling is then nobody's job.  (The
# 0.2/0.3 pin to 22050 made the HAL resample on every modern Mac, which was
# audible as a dull, slightly gritty rendition.)
SAMPLE_RATE = 22050  # default for tests / hermetic gap math
CHANNELS = 1
FORMAT_NAME = "LEI16@22050"  # legacy default; runtime default is detected


def nominal_output_rate(raw: int) -> int:
    """Validate a probed device rate; 48000 (modern-Mac default) on doubt."""
    return raw if 8000 <= raw <= 384000 else 48000


def probe_output_rate() -> int:
    """Raw CoreAudio query of the default output device's rate; 0 on failure."""
    try:
        import ctypes

        class _PropAddr(ctypes.Structure):
            _fields_ = [
                ("sel", ctypes.c_uint32),
                ("scope", ctypes.c_uint32),
                ("elem", ctypes.c_uint32),
            ]

        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_PropAddr),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        out = 0x6F7574  # 'out' scope

        def _get(
            obj: int,
            sel: int,
            size: int,
            ctype: type[ctypes.c_uint32] | type[ctypes.c_double],
        ) -> int:
            addr = _PropAddr(sel, out, 0)
            n = ctypes.c_uint32(size)
            v = ctype()
            st = ca.AudioObjectGetPropertyData(
                obj, ctypes.byref(addr), 0, None, ctypes.byref(n), ctypes.byref(v)
            )
            return int(v.value) if st == 0 else 0

        dev = _get(1, 0x644F7574, 4, ctypes.c_uint32)  # 'dOut' default device
        if dev:
            return _get(dev, 0x6E737274, 8, ctypes.c_double)  # 'nsrt'
        return 0
    except Exception:
        return 0


def detect_output_rate() -> int:
    """Nominal sample rate of the default output device, via CoreAudio.

    Cheap (one property query) and silent.  Falls back to 48000 — the
    default on modern Macs — if the probe fails for any reason.
    """
    return nominal_output_rate(probe_output_rate())


def default_data_format() -> str:
    """Synthesis format matching the output device: e.g. "LEI16@48000"."""
    return f"LEI16@{detect_output_rate()}"


# --------------------------------------------------------------------------- #
# Paragraph splitting                                                         #
# --------------------------------------------------------------------------- #

_SENTENCE_RE = re.compile(r"[^.!?…]*[.!?…]+[\"'”’)\]]*(?:\s+|$)|[^.!?…]+$")


def normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces and strip ends."""
    return " ".join(text.split())


def split_sentences(text: str) -> tuple[str, ...]:
    return tuple(
        m.group(0).strip() for m in _SENTENCE_RE.finditer(text) if m.group(0).strip()
    )


def pack_sentences(sentences: Sequence[str], max_chars: int) -> tuple[str, ...]:
    """Greedily pack sentences into chunks of at most max_chars characters."""

    def pack(chunks: tuple[str, ...], sentence: str) -> tuple[str, ...]:
        if not chunks:
            return (sentence,)
        cur = chunks[-1]
        if len(cur) + 1 + len(sentence) > max_chars:
            return (*chunks, sentence)
        return (*chunks[:-1], f"{cur} {sentence}")

    return reduce(pack, sentences, ())


def split_long_paragraph(text: str, max_chars: int) -> list[str]:
    """Break an over-long paragraph into chunks at sentence boundaries."""
    if max_chars <= 0:
        return [text]
    return list(pack_sentences(split_sentences(text), max_chars)) or [text]


def split_paragraphs(text: str, max_chars: int | None = None) -> list[str]:
    """Split a document into normalized paragraphs.

    Paragraphs are separated by blank lines.  Internal whitespace is
    collapsed to single spaces so the text renders and speaks cleanly.
    If max_chars is given, paragraphs longer than that are further split
    at sentence boundaries.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = (normalize(chunk) for chunk in re.split(r"\n[ \t]*\n+", text))
    expanded = (
        split_long_paragraph(para, max_chars) if max_chars else (para,)
        for para in normalized
        if para
    )
    return list(chain.from_iterable(expanded))


# --------------------------------------------------------------------------- #
# Wrapping                                                                    #
# --------------------------------------------------------------------------- #


def _line_end(text: str, start: int, width: int) -> int:
    """Index just past the wrapped line that begins at `start`."""
    end = min(start + width, len(text))
    if end < len(text):
        sp = text.rfind(" ", start, end + 1)
        if sp > start:
            end = sp
    return end


def _next_line_start(text: str, end: int) -> int:
    """Where the next line begins: one separating space is dropped."""
    return end + 1 if end < len(text) and text[end] == " " else end


def wrap_offsets(text: str, width: int) -> list[tuple[str, int]]:
    """Wrap text to width, keeping each line's offset in the original text."""

    def lines() -> Iterator[tuple[str, int]]:
        """Unfold (line, offset) pairs until the text is exhausted."""
        start = 0
        while start < len(text):
            end = _line_end(text, start, width)
            yield text[start:end], start
            start = _next_line_start(text, end)

    width = max(1, width)
    return list(lines()) or [("", 0)]


# --------------------------------------------------------------------------- #
# Synthesis + cache                                                           #
# --------------------------------------------------------------------------- #


class SynthesisError(Exception):
    """Paragraph audio could not be rendered."""

    def __init__(self, index: int, detail: str) -> None:
        super().__init__(f"paragraph {index + 1}: {detail}")
        self.index = index
        self.detail = detail


def cache_key(
    text: str, voice: str | None, rate: int | None, data_format: str = FORMAT_NAME
) -> str:
    """Stable cache filename stem for a paragraph under voice/rate/format."""
    material = f"{voice or ''}|{rate or ''}|{data_format}|{text}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheFile:
    """One cached WAV as pruning sees it: identity, weight, age."""

    path: Path
    size: int
    mtime: float


def evictions(files: Sequence[CacheFile], limit_bytes: float) -> tuple[Path, ...]:
    """Oldest-first paths whose removal brings the total under limit_bytes.

    Pure policy: no I/O, so the budget arithmetic is testable directly.
    """
    ordered = sorted(files, key=attrgetter("mtime"))
    total = sum(f.size for f in ordered)
    # Remaining total after evicting 0, 1, ... of the oldest files.
    remaining = (
        total - gone for gone in accumulate(chain((0,), (f.size for f in ordered)))
    )
    n = sum(1 for left in remaining if left > limit_bytes)
    return tuple(f.path for f in ordered[:n])


def prune_cache(cache_dir: Path, limit_mb: float) -> None:
    """Remove stale partial renders; evict oldest WAVs over the size limit.

    Call before the synth worker starts (no concurrent renders yet).
    limit_mb <= 0 means unlimited.
    """
    if not cache_dir.is_dir():
        return
    for part in cache_dir.glob("*.part"):
        part.unlink(missing_ok=True)  # interrupted renders
    if limit_mb <= 0:
        return
    wavs = ((p, p.stat()) for p in cache_dir.glob("*.wav") if p.is_file())
    files = [CacheFile(p, st.st_size, st.st_mtime) for p, st in wavs]
    for path in evictions(files, limit_mb * 1024 * 1024):
        path.unlink(missing_ok=True)


class SynthWorker(threading.Thread):
    """Renders paragraphs to cached WAV files ahead of playback.

    A single worker thread keeps the window [cursor, cursor + ahead]
    synthesized — current paragraph first — so playback normally finds its
    file already on disk and never waits on synthesis.  The main thread
    calls ensure(), which returns immediately for cached paragraphs and
    otherwise blocks until the file appears or synthesis fails.
    """

    def __init__(
        self,
        paras: Sequence[str],
        keys: Sequence[str],
        cache_dir: Path,
        say_cmd: Sequence[str],
        ahead: int = 3,
    ) -> None:
        super().__init__(daemon=True)
        self._paras = paras
        self._keys = keys
        self.cache_dir = cache_dir
        self._say_cmd = say_cmd  # say + voice/rate; -o is added per job
        self.ahead = max(0, ahead)
        self._cond = threading.Condition()
        self._cursor = 0
        self._failed: dict[int, str] = {}
        self._stop_flag = False

    # -- main-thread API ----------------------------------------------------

    def path_for(self, idx: int) -> Path:
        return self.cache_dir / f"{self._keys[idx]}.wav"

    def set_cursor(self, idx: int) -> None:
        with self._cond:
            if self._cursor != idx:
                self._cursor = idx
                self._cond.notify_all()

    def clear_failure(self, idx: int) -> None:
        """Forget a failed render so the worker tries it again."""
        with self._cond:
            if self._failed.pop(idx, None) is not None:
                self._cond.notify_all()

    def ensure(self, idx: int) -> Path:
        """Block until idx's audio is cached and return its path.

        Raises SynthesisError if synthesis failed (call clear_failure()
        to make the worker try again) or the worker was stopped.
        """
        with self._cond:
            while True:
                path = self.path_for(idx)
                if path.exists():
                    return path
                if idx in self._failed:
                    raise SynthesisError(idx, self._failed[idx])
                if self._stop_flag:
                    raise SynthesisError(idx, "shutting down")
                self._cond.wait(0.1)

    def stop(self) -> None:
        with self._cond:
            self._stop_flag = True
            self._cond.notify_all()

    # -- worker thread ------------------------------------------------------

    def run(self) -> None:
        while True:
            with self._cond:
                if self._stop_flag:
                    return
                target = self._next_missing()
                if target is None:
                    self._cond.wait(0.1)
                    continue
            self._render(target)

    def _next_missing(self) -> int | None:
        """Smallest window index without cached audio (lock held)."""
        end = min(self._cursor + self.ahead + 1, len(self._keys))
        for idx in range(self._cursor, end):
            if idx not in self._failed and not self.path_for(idx).exists():
                return idx
        return None

    def _render(self, idx: int) -> None:
        """Run one say -o job; record success (file rename) or failure."""
        path = self.path_for(idx)
        tmp = path.with_name(path.name + ".part")
        proc = subprocess.Popen(
            (*self._say_cmd, "-o", str(tmp)),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        err = b""
        stdin = proc.stdin
        if stdin is not None:
            with contextlib.suppress(BrokenPipeError, OSError):
                stdin.write(self._paras[idx].encode("utf-8"))
                stdin.close()  # say died before reading; stderr carries the reason
        stderr = proc.stderr
        if stderr is not None:
            with contextlib.suppress(OSError):
                err = stderr.read() or b""  # EOF when the process exits
        rc = proc.wait()
        ok = rc == 0 and tmp.exists()
        if ok:
            tmp.replace(path)
        with self._cond:
            if not ok:
                self._failed[idx] = " ".join(err.decode("utf-8", "replace").split())[
                    :200
                ]
            self._cond.notify_all()


# --------------------------------------------------------------------------- #
# Playback engines                                                            #
# --------------------------------------------------------------------------- #
#
# The default engine decodes cached WAVs into memory and streams them through
# one long-lived miniaudio device, chaining straight into the next paragraph's
# samples the moment the current ones run out — no silence between paragraphs.
# The pull generator runs on the audio callback thread and must never block:
# when there is nothing to play it yields silence and the device stays open.
#
# The stream itself is an immutable value (StreamState below).  All of its
# transitions — play, prime, stop, pull — are pure functions; ChainEngine is
# the thin effectful shell that guards them with a lock, decodes files, and
# dispatches their events.


@dataclass(frozen=True)
class StreamState:
    """Immutable snapshot of the chained sample stream.

    `sources`/`paths` hold decoded samples and their files for the live
    window; `cur`/`data`/`pos` are the paragraph being streamed; `chained`
    is a decoded next paragraph the stream may roll into; `gap_left` is
    inter-paragraph silence still owed.
    """

    sources: Mapping[int, bytes] = field(default_factory=dict[int, bytes])
    paths: Mapping[int, Path] = field(default_factory=dict[int, Path])
    cur: int | None = None
    data: bytes = b""
    pos: int = 0
    chained: int | None = None
    gap_left: int = 0


def stream_gc(s: StreamState) -> StreamState:
    """Drop samples/paths below the stream's lowest live paragraph."""
    if s.cur is None and s.chained is None:
        return s
    floor = min(i for i in (s.cur, s.chained) if i is not None)
    return replace(
        s,
        sources={i: b for i, b in s.sources.items() if i >= floor},
        paths={i: p for i, p in s.paths.items() if i >= floor},
    )


def stream_play(s: StreamState, idx: int, path: Path, data: bytes) -> StreamState:
    """Start streaming paragraph idx from freshly decoded samples."""
    return stream_gc(
        replace(
            s,
            sources={**s.sources, idx: data},
            paths={**s.paths, idx: path},
            cur=idx,
            data=data,
            pos=0,
            chained=None,
        )
    )


def stream_prime(s: StreamState, idx: int, path: Path, data: bytes) -> StreamState:
    """Register decoded-ahead samples; chain them if they directly follow."""
    chained = idx if s.cur is not None and idx == s.cur + 1 else s.chained
    return replace(
        s,
        sources={**s.sources, idx: data},
        paths={**s.paths, idx: path},
        chained=chained,
    )


def stream_stop(s: StreamState) -> StreamState:
    """Go idle.  The device stays open; replay starts from stream_play."""
    return stream_gc(replace(s, cur=None, data=b"", pos=0, chained=None, gap_left=0))


def stream_next_chunk(
    s: StreamState,
    want: int,
    gap_bytes: int,
    fails_at: Callable[[int], bool],
) -> tuple[bytes | None, StreamState, tuple[tuple[Any, ...], ...]]:
    """Pure core of the stream pull: the next `want` bytes of audio.

    Paragraph boundaries are transparent: a single request can span the
    end of one paragraph, the configured inter-paragraph gap, and the
    start of the chained next one, so the stream is continuous at any
    request size.

    Returns (chunk, successor state, events).  `chunk` is None when the
    stream is idle.  "started" is an internal event the engine performs
    as its `_on_started` hook instead of queueing it.
    """
    state = s
    parts: tuple[bytes, ...] = ()
    events: tuple[tuple[Any, ...], ...] = ()
    need = want
    while need > 0:
        if state.gap_left > 0:
            take = min(need, state.gap_left)
            parts += (b"\x00" * take,)
            state = replace(state, gap_left=state.gap_left - take)
            need -= take
            continue
        if state.cur is None:
            break
        cur = state.cur
        if state.pos >= len(state.data):
            nxt = state.chained
            chained = nxt is not None and nxt in state.sources
            events += (("finished", cur, chained),)
            if nxt is None or nxt not in state.sources:
                state = stream_stop(state)
                break
            state = stream_gc(
                replace(
                    state,
                    cur=nxt,
                    data=state.sources[nxt],
                    pos=0,
                    chained=None,
                    gap_left=gap_bytes,
                )
            )
            events += (("chained", nxt),)
            continue
        if state.pos == 0:
            if fails_at(cur):
                events += (("crashed", cur, "simulated device error"),)
                state = replace(state, cur=None, data=b"", pos=0, chained=None)
                break
            events += (("started", cur),)
        take = min(need, len(state.data) - state.pos)
        parts += (state.data[state.pos : state.pos + take],)
        state = replace(state, pos=state.pos + take)
        need -= take
    if not parts:
        return None, state, events
    return b"".join(parts), state, events


class ChainEngine:
    """Continuous chunk-stream playback with zero-gap paragraph chaining.

    Subclasses provide `_load` (file → raw s16 mono samples) and, for real
    audio output, start a consumer that pulls from `_pull`.  The main thread
    observes progress through `current_index()` and failures through
    `events` ("crashed", idx, detail).
    """

    def __init__(self, chunk_bytes: int = 1102, gap_ms: int = 0) -> None:
        # ~25 ms chunks @ 22 kHz mono s16
        self._chunk_bytes = chunk_bytes
        self._rate = SAMPLE_RATE
        self._channels = CHANNELS
        self._gap_ms = gap_ms
        self._recompute_gap()
        self._lock = threading.Lock()
        self.events: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self._stream = StreamState()
        self._closed = False

    # -- main-thread API ----------------------------------------------------

    def play(self, idx: int, path: Path) -> None:
        """Start streaming paragraph idx from its cached file."""
        data = self._load(path)
        with self._lock:
            self._stream = stream_play(self._stream, idx, path, data)

    def prime(self, idx: int, path: Path) -> None:
        """Decode a paragraph ahead of time so the chain can reach it."""
        try:
            data = self._load(path)
        except Exception:
            return  # best effort; the fallback path will retry via play()
        with self._lock:
            self._stream = stream_prime(self._stream, idx, path, data)

    def stop_stream(self) -> None:
        """Stop playback.  The device stays open; replay is instant."""
        with self._lock:
            self._stream = stream_stop(self._stream)

    def current_index(self) -> int | None:
        """The paragraph currently streaming, or None when idle/finished."""
        with self._lock:
            return self._stream.cur

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stream = stream_stop(self._stream)
        self._on_close()

    # -- hooks for subclasses ------------------------------------------------

    @property
    def _paths(self) -> Mapping[int, Path]:
        """Live paragraphs' paths (TestEngine's start log reads this)."""
        return self._stream.paths

    def _load(self, path: Path) -> bytes:
        raise NotImplementedError

    def _on_started(self, idx: int) -> None:
        """Called (consumer thread) when streaming of idx actually begins."""

    def _fail_idx(self, idx: int) -> bool:
        """Return True to simulate a device error at the start of idx."""
        return False

    def _on_close(self) -> None:
        pass

    # -- consumer thread -----------------------------------------------------

    def _pull(self) -> Iterator[bytes]:
        """Yield fixed-size chunks forever (test consumers; no protocol)."""
        while not self._closed:
            chunk = self._next_chunk(self._chunk_bytes)
            yield chunk if chunk is not None else b"\x00" * self._chunk_bytes

    def _pull_frames(self) -> Generator[bytes, int | None, None]:
        """Protocol generator for the miniaudio device.

        The device callback sends in the number of frames it wants and we
        must answer with exactly that many frames (s16 mono) — a short
        answer leaves the rest of the period unfilled and playback stalls.
        """
        frames = yield b""
        while not self._closed:
            want = max(int(frames or 0), 1) * self._channels * 2
            chunk = self._next_chunk(want)
            if chunk is None:
                chunk = bytes(want)
            elif len(chunk) < want:
                chunk += bytes(want - len(chunk))
            frames = yield chunk

    def _recompute_gap(self) -> None:
        gap_frames = int(round(self._gap_ms * self._rate / 1000))
        self._gap_bytes = gap_frames * self._channels * 2

    def set_stream_format(self, rate: int, channels: int) -> None:
        """Align byte math with the actual stream (before device open)."""
        self._rate = rate
        self._channels = channels
        self._recompute_gap()

    def _next_chunk(self, want: int) -> bytes | None:
        """Apply one pure stream step; dispatch its events under the lock."""
        with self._lock:
            chunk, self._stream, step_events = stream_next_chunk(
                self._stream, want, self._gap_bytes, self._fail_idx
            )
            for ev in step_events:
                if ev[0] == "started":
                    self._on_started(ev[1])
                else:
                    self.events.put(ev)
        return chunk


class MiniaudioEngine(ChainEngine):
    """ChainEngine over a single long-lived miniaudio output device."""

    def __init__(self, chunk_bytes: int = 1102, gap_ms: int = 0) -> None:
        super().__init__(chunk_bytes, gap_ms)
        self._device: Any = None

    def _load(self, path: Path) -> bytes:
        decoded = miniaudio.wav_read_file_s16(str(path))
        return cast(bytes, decoded.samples.tobytes())

    def play(self, idx: int, path: Path) -> None:
        self._ensure_device(path)
        super().play(idx, path)

    def _ensure_device(self, path: Path) -> None:
        if self._device is not None:
            return
        # Open the device with the cached files' actual rate/channels: the
        # device then runs natively and no resampling happens anywhere.
        with wave.open(str(path), "rb") as w:
            rate, channels = w.getframerate(), w.getnchannels()
        self.set_stream_format(rate, channels)
        self._device = miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=channels,
            sample_rate=rate,
            buffersize_msec=60,  # snappier pause than the 200 ms default
        )
        pull = self._pull_frames()
        next(pull)  # prime: advance to the first yield before start()
        self._device.start(pull)

    def _on_close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None


class TestEngine(ChainEngine):
    """Headless ChainEngine for subprocess tests.

    Consumes its own chunk stream on a drain thread at a configurable rate.
    Environment:
        T2S_TEST_PLAY_LOG      append played paths (started streams)
        T2S_TEST_PLAY_DELAY    seconds per chunk (default 0.05)
        T2S_TEST_PLAY_FAIL_AT  1-based stream start that fails instead of
                               playing (simulates a device error)
    """

    def __init__(self, gap_ms: int = 0) -> None:
        super().__init__(gap_ms=gap_ms)
        self._delay = float(os.environ.get("T2S_TEST_PLAY_DELAY", "0.05") or 0)
        self._fail_at = int(os.environ.get("T2S_TEST_PLAY_FAIL_AT", "0") or 0)
        self._log_path = os.environ.get("T2S_TEST_PLAY_LOG")
        self._started_no = 0
        self._drain: threading.Thread | None = None

    def _load(self, path: Path) -> bytes:
        with wave.open(str(path), "rb") as w:
            return w.readframes(w.getnframes())

    def _fail_idx(self, idx: int) -> bool:
        self._started_no += 1
        return self._started_no == self._fail_at

    def _on_started(self, idx: int) -> None:
        if self._log_path:
            with open(self._log_path, "a") as f:
                f.write(f"{self._paths[idx]}\n")

    def play(self, idx: int, path: Path) -> None:
        super().play(idx, path)
        if self._drain is None or not self._drain.is_alive():
            self._drain = threading.Thread(target=self._drain_loop, daemon=True)
            self._drain.start()

    def _drain_loop(self) -> None:
        for _ in self._pull():
            if self._delay:
                time.sleep(self._delay)


class SubprocessEngine:
    """afplay fallback: one external process per paragraph (with gaps)."""

    def __init__(self, play_cmd: Sequence[str], gap_ms: int = 0) -> None:
        self._cmd = play_cmd
        self._gap_ms = gap_ms  # not applicable: gaps live inside the stream
        self.events: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self._proc: subprocess.Popen[bytes] | None = None
        self._idx: int | None = None
        self._reported = True

    def play(self, idx: int, path: Path) -> None:
        self.stop_stream()
        self._proc = subprocess.Popen(
            (*self._cmd, str(path)), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        self._idx = idx
        self._reported = False
        threading.Thread(
            target=self._watch, args=(idx, self._proc), daemon=True
        ).start()

    def _watch(self, idx: int, proc: subprocess.Popen[bytes]) -> None:
        """Push the exit event for one spawned player process."""
        rc = proc.wait()
        if self._reported:
            return  # user-initiated stop (or superseded by a newer play)
        if rc == 0:
            self.events.put(("finished", idx, False))
        else:
            stderr = proc.stderr.read() if proc.stderr is not None else b""
            tail = " ".join(stderr.decode("utf-8", "replace").split())[:200]
            self.events.put(("crashed", idx, f"player exited with code {rc}: {tail}"))

    def stop_stream(self) -> None:
        p = self._proc
        if p is not None and p.poll() is None:
            self._reported = True  # user-initiated stop: never a "crash"
            with contextlib.suppress(ProcessLookupError):
                p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        self._idx = None

    def current_index(self) -> int | None:
        """The paragraph whose player process is still running, if any."""
        p = self._proc
        if p is None or self._idx is None:
            return None
        if p.poll() is None:
            return self._idx
        self._idx = None
        return None

    def prime(self, idx: int, path: Path) -> None:
        pass

    def close(self) -> None:
        self.stop_stream()


def make_engine(
    player: str, play_cmd: Sequence[str], gap_ms: int = 0
) -> ChainEngine | SubprocessEngine:
    if player == "afplay":
        return SubprocessEngine(play_cmd, gap_ms)
    if player == "test":
        return TestEngine(gap_ms)
    if HAVE_MINIAUDIO:
        return MiniaudioEngine(gap_ms=gap_ms)
    if player == "miniaudio":
        print(
            "t2s: --player miniaudio but the 'miniaudio' package is not installed",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(
        "t2s: miniaudio is not installed — falling back to afplay "
        "(pip install miniaudio for gapless playback)",
        file=sys.stderr,
    )
    return SubprocessEngine(play_cmd)


# --------------------------------------------------------------------------- #
# Application                                                                 #
# --------------------------------------------------------------------------- #

PlayState = Literal["playing", "paused", "stopped"]
PlayOutcome = Literal["playing", "paused", "failed"]


def build_say_cmd(
    say_bin: str, voice: str | None, rate: int | None, data_format: str
) -> tuple[str, ...]:
    """The say invocation that renders one paragraph (text on stdin) to WAV."""
    options = (
        ("-v", voice) if voice else (),
        ("-r", str(rate)) if rate else (),
        ("--file-format", "WAVE"),
        ("--data-format", data_format),
    )
    return (say_bin, *chain.from_iterable(options))


def done_message(count: int, had_errors: bool) -> str:
    """The final status line once the document has finished."""
    if had_errors:
        return "✓ done (with errors)"
    return f"✓ done — {count} paragraph{'s' if count != 1 else ''}"


class App:
    """Owns the paragraph queue, playback state, keyboard and display."""

    def __init__(
        self,
        paras: list[str],
        start_idx: int = 0,
        width: int = 72,
        voice: str | None = None,
        rate: int | None = None,
        say_bin: str | None = None,
        play_bin: str | None = None,
        cache_dir: str | None = None,
        cache_limit_mb: float = 256.0,
        ahead: int = 3,
        player: str = "auto",
        gap_ms: int = 0,
        data_format: str | None = None,
    ) -> None:
        self.paras = paras
        self.idx = start_idx
        self.width = width
        self.ansi = sys.stdout.isatty()
        self.state: PlayState = "stopped"
        self.running = False
        self._had_errors = False

        say_bin = say_bin or os.environ.get("T2S_SAY_BIN") or "say"
        fmt = data_format or default_data_format()
        say_cmd = build_say_cmd(say_bin, voice, rate, fmt)
        keys = tuple(cache_key(p, voice, rate, fmt) for p in paras)
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir
            else (Path.home() / "Library" / "Caches" / "t2s")
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        prune_cache(self.cache_dir, cache_limit_mb)
        self.synth = SynthWorker(paras, keys, self.cache_dir, say_cmd, ahead=ahead)

        play_cmd = (play_bin or os.environ.get("T2S_PLAY_BIN") or "afplay",)
        self.engine = make_engine(player, play_cmd, gap_ms)

        # Keyboard source: stdin when it is a terminal, otherwise the
        # controlling terminal (so `cat book.txt | t2s` stays interactive).
        # Without either, t2s runs non-interactively to the end.
        self.key_fd: int | None = None
        if sys.stdin.isatty():
            self.key_fd = sys.stdin.fileno()
        else:
            try:
                self.key_fd = os.open("/dev/tty", os.O_RDONLY)
            except OSError:
                self.key_fd = None
        self._termios_fd: int | None = None
        self._termios_old: list[int | list[bytes | int]] | None = None

    # -- display ------------------------------------------------------------

    def show_paragraph(self, note: str | None = None) -> None:
        header = f"── ¶ {self.idx + 1}/{len(self.paras)}"
        if note:
            header += f" {note}"
        header += " ──"
        print(self._dim(header))
        for line, _ in wrap_offsets(self.paras[self.idx], self.width):
            print(line)
        sys.stdout.flush()

    # -- playback -----------------------------------------------------------

    def play(self, idx: int, note: str | None = None) -> PlayOutcome:
        """Start playing paragraph idx.

        Returns "playing", "paused" (interactive synthesis failure — wait
        for the user) or "failed" (synthesis failed; non-interactive mode
        should move on).
        """
        self.engine.stop_stream()
        self.idx = idx
        self.synth.set_cursor(idx)
        self.show_paragraph(note)
        try:
            path = self.synth.ensure(idx)
        except SynthesisError as exc:
            self._had_errors = True
            msg = f"! could not render paragraph: {exc.detail}"
            if self.key_fd is not None:
                self._warn(msg)
                self._status("space: retry · n/p: paragraph · q: quit")
                self.state = "paused"
                return "paused"
            self._warn(msg + " — continuing with next paragraph")
            self.state = "stopped"
            return "failed"
        self.engine.play(idx, path)
        self.state = "playing"
        self._prime_next()
        return "playing"

    def _prime_next(self) -> None:
        """Decode the next paragraph ahead of time so the chain reaches it."""
        nxt = self.idx + 1
        if nxt < len(self.paras):
            path = self.synth.path_for(nxt)
            if path.exists():
                self.engine.prime(nxt, path)

    def _sync_to(self, idx: int) -> None:
        """The engine chained into paragraph idx on its own — catch up."""
        self.idx = idx
        self.synth.set_cursor(idx)
        self.show_paragraph()
        self._prime_next()

    def _advance(self, after_error: bool = False) -> None:
        """Move to the next paragraph, or finish the document.

        In non-interactive mode, synthesis failures are skipped over here
        (iteratively — a document where every render fails must not
        recurse one stack frame per paragraph).
        """
        nxt = self.idx + 1
        while nxt < len(self.paras):
            if self.play(nxt) == "playing":
                return
            self._had_errors = True
            nxt += 1
        self.running = False
        self._note(done_message(len(self.paras), after_error or self._had_errors))

    def run(self) -> int:
        self.running = True
        if self.key_fd is not None:
            try:
                self._termios_old = termios.tcgetattr(self.key_fd)
                tty.setcbreak(self.key_fd)
                self._termios_fd = self.key_fd
            except termios.error:
                self._termios_old = None
                self._termios_fd = None
                self.key_fd = None
        self.synth.start()
        try:
            if self.play(self.idx) == "failed" and self.key_fd is None:
                self._advance(after_error=True)
            while self.running:
                self._wait()
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

    # -- event loop ---------------------------------------------------------

    def _wait(self) -> None:
        """Wait for a keypress, then sync with the engine."""
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
                self._on_key(ch)
        self._poll_engine()

    def _poll_engine(self) -> None:
        """Process engine events: failures, paragraph ends, chain syncs.

        Advancement is driven by the event queue (FIFO, nothing can be
        missed) rather than by polling, so a paragraph that starts and
        finishes between two ticks is never replayed or skipped.
        """
        while True:
            try:
                ev = self.engine.events.get_nowait()
            except queue.Empty:
                break
            kind = ev[0]
            if kind == "crashed":
                if self.state != "playing":
                    continue
                self._had_errors = True
                msg = f"! playback failed: {ev[2]}"
                if self.key_fd is not None:
                    self._warn(msg)
                    self._status("⏸ device error — space: replay · n/p: skip · q: quit")
                    self.state = "paused"
                else:
                    self._warn(msg + " — continuing with next paragraph")
                    self._advance(after_error=True)
                    return
            elif kind == "finished":
                idx, chained = ev[1], ev[2]
                if self.state != "playing":
                    continue
                self.idx = idx
                # `chained` is decided atomically at the stream boundary by
                # the engine, so event pile-ups (several short paragraphs
                # ending inside one tick) can never cause a replay.
                if chained:
                    continue  # the "chained" event syncs us forward
                if idx + 1 < len(self.paras):
                    if self.play(idx + 1) == "failed":
                        self._advance(after_error=True)
                        return
                else:
                    self.running = False
                    self._note(done_message(len(self.paras), self._had_errors))
            elif kind == "chained":
                if self.state == "playing":
                    self._sync_to(ev[1])

    # -- keyboard -----------------------------------------------------------

    def _on_key(self, ch: str) -> None:
        if ch in ("q", "Q", "\x03"):
            self.running = False
            self._note(f"· stopped at ¶ {self.idx + 1}/{len(self.paras)}")
        elif ch == " ":
            if self.state == "playing":
                if self.engine.current_index() is not None:
                    self.engine.stop_stream()
                    self.state = "paused"
                    self._status(
                        "⏸ paused — space: replay paragraph · n/p: paragraph · q: quit"
                    )
                # else: the paragraph already finished; the next tick advances
            elif self.state == "paused":
                self.synth.clear_failure(self.idx)
                self.play(self.idx, note="· resumed")
        elif ch in ("n", "N", "p", "P"):
            delta = 1 if ch in ("n", "N") else -1
            target = self.idx + delta
            if not 0 <= target < len(self.paras):
                edge = "last" if delta > 0 else "first"
                self._status(f"· already at {edge} paragraph")
                return
            self.play(target)

    # -- output helpers -----------------------------------------------------

    def _dim(self, text: str) -> str:
        return f"{DIM}{text}{RESET}" if self.ansi else text

    def _note(self, text: str) -> None:
        print(self._dim(text))
        sys.stdout.flush()

    def _status(self, text: str) -> None:
        self._note(text)

    def _warn(self, text: str) -> None:
        print(text, file=sys.stderr)
        sys.stderr.flush()


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="t2s",
        description="Read a document aloud with macOS say(1), paragraph by "
        "paragraph (space: pause/replay, n/p: skip, q: quit). "
        "Paragraphs are synthesized ahead to a cache and played "
        "with afplay.",
    )
    p.add_argument(
        "file",
        nargs="?",
        default="-",
        help="text file to read ('-' or omitted: standard input)",
    )
    p.add_argument("-v", "--voice", help="voice name passed to say")
    p.add_argument(
        "-r", "--rate", type=int, metavar="WPM", help="speech rate in words per minute"
    )
    p.add_argument(
        "--width",
        type=int,
        default=72,
        metavar="COLS",
        help="display wrap width (default: 72)",
    )
    p.add_argument(
        "--start",
        type=int,
        default=1,
        metavar="N",
        help="paragraph number to start from (1-based)",
    )
    p.add_argument(
        "--split-long",
        type=int,
        default=None,
        metavar="CHARS",
        help="also split paragraphs longer than CHARS at sentence boundaries",
    )
    p.add_argument(
        "--ahead",
        type=int,
        default=3,
        metavar="N",
        help="paragraphs to synthesize ahead of playback "
        "(default: 3; raise for slow premium voices)",
    )
    p.add_argument(
        "--cache-dir",
        default=None,
        metavar="PATH",
        help="audio cache directory (default: ~/Library/Caches/t2s)",
    )
    p.add_argument(
        "--cache-limit-mb",
        type=float,
        default=256.0,
        metavar="MB",
        help="prune the cache when larger than this (default: 256; 0 = unlimited)",
    )
    p.add_argument(
        "--say-bin",
        default=None,
        metavar="PATH",
        help="say binary to run (default: say, or $T2S_SAY_BIN); "
        "useful for testing with a fake",
    )
    p.add_argument(
        "--play-bin",
        default=None,
        metavar="PATH",
        help="audio player binary (default: afplay, or $T2S_PLAY_BIN)",
    )
    p.add_argument(
        "--gap",
        type=int,
        default=0,
        metavar="MS",
        help="silence between paragraphs in milliseconds (default: 0)",
    )
    p.add_argument(
        "--data-format",
        default=None,
        metavar="FMT",
        help="synthesis format for say, e.g. LEI16@48000 "
        "(default: LEI16 at the output device's native rate)",
    )
    p.add_argument(
        "--player",
        default="auto",
        choices=["auto", "miniaudio", "afplay", "test"],
        help="playback engine: miniaudio (gapless in-process "
        "streaming), afplay (external process fallback), "
        "test (headless, for t2s's own tests), or auto "
        "(default: miniaudio, falls back to afplay)",
    )
    p.add_argument("--version", action="version", version=__version__)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.file == "-":
        if sys.stdin.isatty():
            print(
                "t2s: no input — pass a file path or pipe text (see t2s --help)",
                file=sys.stderr,
            )
            return 2
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"t2s: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2

    paras = split_paragraphs(text, args.split_long)
    if not paras:
        print("t2s: input contains no text", file=sys.stderr)
        return 1
    if not 1 <= args.start <= len(paras):
        print(
            f"t2s: --start {args.start} is out of range "
            f"(document has {len(paras)} paragraphs)",
            file=sys.stderr,
        )
        return 2

    app = App(
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
    try:
        return app.run()
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
