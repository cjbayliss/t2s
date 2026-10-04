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
    engine_choice,
    frames_to_bytes,
    gap_bytes,
    nominal_output_rate,
    stream_next_chunk,
    stream_play,
    stream_prime,
    stream_stop,
)

try:
    import miniaudio

    HAVE_MINIAUDIO = True
except ImportError:
    miniaudio = None
    HAVE_MINIAUDIO = False


_PROP_DEFAULT_OUTPUT_DEVICE = 0x644F7574
_PROP_NOMINAL_SAMPLE_RATE = 0x6E737274
_SCOPE_OUTPUT = 0x6F7574


def probe_output_rate() -> int:
    try:
        import ctypes

        class _PropertyAddress(ctypes.Structure):
            _fields_ = [
                ("selector", ctypes.c_uint32),
                ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32),
            ]

        core_audio = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )
        core_audio.AudioObjectGetPropertyData.restype = ctypes.c_int32
        core_audio.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_PropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]

        def _get_property(
            object_id: int,
            selector: int,
            size: int,
            ctype: type[ctypes.c_uint32] | type[ctypes.c_double],
        ) -> int:
            address = _PropertyAddress(selector, _SCOPE_OUTPUT, 0)
            data_size = ctypes.c_uint32(size)
            value = ctype()
            status = core_audio.AudioObjectGetPropertyData(
                object_id,
                ctypes.byref(address),
                0,
                None,
                ctypes.byref(data_size),
                ctypes.byref(value),
            )
            return int(value.value) if status == 0 else 0

        device_id = _get_property(1, _PROP_DEFAULT_OUTPUT_DEVICE, 4, ctypes.c_uint32)
        if device_id:
            return _get_property(
                device_id, _PROP_NOMINAL_SAMPLE_RATE, 8, ctypes.c_double
            )
        return 0
    except (OSError, AttributeError):
        return 0


def detect_output_rate() -> int:
    return nominal_output_rate(probe_output_rate())


def default_data_format() -> str:
    return f"LEI16@{detect_output_rate()}"


class ChainEngine:
    def __init__(
        self, chunk_bytes: int = 1102, gap_ms: int = 0, fail_at: int = 0
    ) -> None:
        self._chunk_bytes = chunk_bytes
        self._channels = CHANNELS
        self._gap_ms = gap_ms
        self._gap_bytes = gap_bytes(gap_ms, SAMPLE_RATE, CHANNELS)
        self._fail_at = fail_at
        self._lock = threading.Lock()
        self.events: queue.SimpleQueue[EngineEvent] = queue.SimpleQueue()
        self._stream = StreamState()
        self._closed = False

    def play(self, index: int, path: Path) -> None:
        data = self._load(path)
        with self._lock:
            self._stream = stream_play(self._stream, index, path, data)

    def prime(self, index: int, path: Path) -> None:
        try:
            data = self._load(path)
        except (OSError, EOFError, wave.Error, RuntimeError):
            return
        with self._lock:
            self._stream = stream_prime(self._stream, index, path, data)

    def stop_stream(self) -> None:
        with self._lock:
            self._stream = stream_stop(self._stream)

    def current_index(self) -> int | None:
        with self._lock:
            return self._stream.current_index

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stream = stream_stop(self._stream)
        self._on_close()

    @property
    def _paths(self) -> Mapping[int, Path]:
        return self._stream.paths

    def _load(self, path: Path) -> bytes:
        raise NotImplementedError

    def _on_started(self, index: int) -> None:
        pass

    def _on_close(self) -> None:
        pass

    def _pull(self) -> Iterator[bytes]:
        while not self._closed:
            chunk = self._next_chunk(self._chunk_bytes)
            yield chunk if chunk is not None else b"\x00" * self._chunk_bytes

    def _pull_frames(self) -> Generator[bytes, int | None, None]:
        frames = yield b""
        while not self._closed:
            want_bytes = frames_to_bytes(max(int(frames or 0), 1), self._channels)
            chunk = self._next_chunk(want_bytes)
            if chunk is None:
                chunk = bytes(want_bytes)
            elif len(chunk) < want_bytes:
                chunk += bytes(want_bytes - len(chunk))
            frames = yield chunk

    def set_stream_format(self, rate: int, channels: int) -> None:
        self._channels = channels
        self._gap_bytes = gap_bytes(self._gap_ms, rate, channels)

    def _next_chunk(self, want_bytes: int) -> bytes | None:
        with self._lock:
            chunk, self._stream, step_events = stream_next_chunk(
                self._stream, want_bytes, self._gap_bytes, self._fail_at
            )
            for event in step_events:
                match event:
                    case StreamStarted():
                        self._on_started(event.index)
                    case _:
                        self.events.put(event)
        return chunk


class MiniaudioEngine(ChainEngine):
    def __init__(self, chunk_bytes: int = 1102, gap_ms: int = 0) -> None:
        super().__init__(chunk_bytes, gap_ms)
        self._device: Any = None

    def _load(self, path: Path) -> bytes:
        decoded = miniaudio.wav_read_file_s16(str(path))
        return cast(bytes, decoded.samples.tobytes())

    def play(self, index: int, path: Path) -> None:
        self._ensure_device(path)
        super().play(index, path)

    def _ensure_device(self, path: Path) -> None:
        if self._device is not None:
            return
        with wave.open(str(path), "rb") as wav:
            rate, channels = wav.getframerate(), wav.getnchannels()
        self.set_stream_format(rate, channels)
        self._device = miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=channels,
            sample_rate=rate,
            buffersize_msec=60,
        )
        pull = self._pull_frames()
        next(pull)
        self._device.start(pull)

    def _on_close(self) -> None:
        if self._device is not None:
            self._device.close()
            self._device = None


class TestEngine(ChainEngine):
    def __init__(self, gap_ms: int = 0, env: Mapping[str, str] | None = None) -> None:
        env_vars = env if env is not None else {}
        super().__init__(
            gap_ms=gap_ms,
            fail_at=int(env_vars.get("T2S_TEST_PLAY_FAIL_AT", "0") or 0),
        )
        self._delay = float(env_vars.get("T2S_TEST_PLAY_DELAY", "0.05") or 0)
        self._log_path = env_vars.get("T2S_TEST_PLAY_LOG")
        self._drain_thread: threading.Thread | None = None

    def _load(self, path: Path) -> bytes:
        with wave.open(str(path), "rb") as wav:
            return wav.readframes(wav.getnframes())

    def _on_started(self, index: int) -> None:
        if self._log_path:
            with open(self._log_path, "a") as log_file:
                log_file.write(f"{self._paths[index]}\n")

    def play(self, index: int, path: Path) -> None:
        super().play(index, path)
        if self._drain_thread is None or not self._drain_thread.is_alive():
            self._drain_thread = threading.Thread(target=self._drain_loop, daemon=True)
            self._drain_thread.start()

    def _drain_loop(self) -> None:
        for _ in self._pull():
            if self._delay:
                time.sleep(self._delay)


class SubprocessEngine:
    def __init__(self, play_cmd: Sequence[str], gap_ms: int = 0) -> None:
        self._cmd = play_cmd
        self._gap_ms = gap_ms
        self.events: queue.SimpleQueue[EngineEvent] = queue.SimpleQueue()
        self._proc: subprocess.Popen[bytes] | None = None
        self._current_index: int | None = None
        self._suppress_report = True

    def play(self, index: int, path: Path) -> None:
        self.stop_stream()
        self._proc = subprocess.Popen(
            (*self._cmd, str(path)), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        self._current_index = index
        self._suppress_report = False
        threading.Thread(
            target=self._watch, args=(index, self._proc), daemon=True
        ).start()

    def _watch(self, index: int, proc: subprocess.Popen[bytes]) -> None:
        exit_code = proc.wait()
        if self._suppress_report:
            return
        if exit_code == 0:
            self.events.put(StreamFinished(index, False))
        else:
            stderr = proc.stderr.read() if proc.stderr is not None else b""
            tail = " ".join(stderr.decode("utf-8", "replace").split())[:200]
            self.events.put(
                StreamCrashed(index, f"player exited with code {exit_code}: {tail}")
            )

    def stop_stream(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._suppress_report = True
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._current_index = None

    def current_index(self) -> int | None:
        proc = self._proc
        if proc is None or self._current_index is None:
            return None
        return self._current_index if proc.poll() is None else None

    def prime(self, index: int, path: Path) -> None:
        pass

    def close(self) -> None:
        self.stop_stream()


def make_engine(
    player: str,
    play_cmd: Sequence[str],
    gap_ms: int = 0,
    env: Mapping[str, str] | None = None,
) -> ChainEngine | SubprocessEngine:
    choice = engine_choice(player, HAVE_MINIAUDIO)
    if choice == "missing-miniaudio":
        print(
            "t2s: --player miniaudio but the 'miniaudio' package is not installed",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if choice == "afplay-fallback":
        print(
            "t2s: miniaudio is not installed — falling back to afplay "
            "(pip install miniaudio for gapless playback)",
            file=sys.stderr,
        )
    if choice == "miniaudio":
        return MiniaudioEngine(gap_ms=gap_ms)
    if choice == "test":
        return TestEngine(gap_ms, env)
    return SubprocessEngine(play_cmd, gap_ms)
