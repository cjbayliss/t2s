"""Playback engines: effectful shells around the pure stream state.

ChainEngine guards the pure transitions with a lock, decodes cached
files, and dispatches their typed events; MiniaudioEngine adds the
long-lived output device, TestEngine is a headless consumer driven by
environment (passed in, never read at import), and SubprocessEngine is
the afplay fallback.  The CoreAudio rate probe lives here too — it is
pure effect, queried once at startup.
"""

from __future__ import annotations

import contextlib
import queue
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Generator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from .pure import (
    CHANNELS,
    SAMPLE_RATE,
    EngineEvent,
    StreamCrashed,
    StreamFinished,
    StreamStarted,
    StreamState,
    nominal_output_rate,
    stream_next_chunk,
    stream_play,
    stream_prime,
    stream_stop,
)

try:
    import miniaudio

    HAVE_MINIAUDIO = True
except ImportError:  # pragma: no cover - environment dependent
    miniaudio = None
    HAVE_MINIAUDIO = False


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


class ChainEngine:
    """Continuous chunk-stream playback with zero-gap paragraph chaining.

    Subclasses provide `_load` (file → raw s16 mono samples) and, for real
    audio output, start a consumer that pulls from `_pull`.  The main thread
    observes progress through `current_index()` and failures through
    `events` (StreamCrashed).
    """

    def __init__(
        self, chunk_bytes: int = 1102, gap_ms: int = 0, fail_at: int = 0
    ) -> None:
        # ~25 ms chunks @ 22 kHz mono s16
        self._chunk_bytes = chunk_bytes
        self._rate = SAMPLE_RATE
        self._channels = CHANNELS
        self._gap_ms = gap_ms
        self._recompute_gap()
        self._fail_at = fail_at  # 1-based stream start to fail (crash injection)
        self._lock = threading.Lock()
        self.events: queue.SimpleQueue[EngineEvent] = queue.SimpleQueue()
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
                self._stream, want, self._gap_bytes, self._fail_at
            )
            for ev in step_events:
                match ev:
                    case StreamStarted():
                        self._on_started(ev.idx)
                    case _:
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
    Environment (passed in by the caller, never read from os.environ here):
        T2S_TEST_PLAY_LOG      append played paths (started streams)
        T2S_TEST_PLAY_DELAY    seconds per chunk (default 0.05)
        T2S_TEST_PLAY_FAIL_AT  1-based stream start that fails instead of
                               playing (simulates a device error)
    """

    def __init__(self, gap_ms: int = 0, env: Mapping[str, str] | None = None) -> None:
        env_vars = env if env is not None else {}
        super().__init__(
            gap_ms=gap_ms,
            fail_at=int(env_vars.get("T2S_TEST_PLAY_FAIL_AT", "0") or 0),
        )
        self._delay = float(env_vars.get("T2S_TEST_PLAY_DELAY", "0.05") or 0)
        self._log_path = env_vars.get("T2S_TEST_PLAY_LOG")
        self._drain: threading.Thread | None = None

    def _load(self, path: Path) -> bytes:
        with wave.open(str(path), "rb") as w:
            return w.readframes(w.getnframes())

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
        self.events: queue.SimpleQueue[EngineEvent] = queue.SimpleQueue()
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
            self.events.put(StreamFinished(idx, False))
        else:
            stderr = proc.stderr.read() if proc.stderr is not None else b""
            tail = " ".join(stderr.decode("utf-8", "replace").split())[:200]
            self.events.put(StreamCrashed(idx, f"player exited with code {rc}: {tail}"))

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
        return self._idx if p.poll() is None else None

    def prime(self, idx: int, path: Path) -> None:
        pass

    def close(self) -> None:
        self.stop_stream()


def make_engine(
    player: str,
    play_cmd: Sequence[str],
    gap_ms: int = 0,
    env: Mapping[str, str] | None = None,
) -> ChainEngine | SubprocessEngine:
    if player == "afplay":
        return SubprocessEngine(play_cmd, gap_ms)
    if player == "test":
        return TestEngine(gap_ms, env)
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
