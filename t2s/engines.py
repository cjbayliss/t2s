from __future__ import annotations

import contextlib
import queue
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .pure import (
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


def probe_output_rate() -> int:
    prop_default_output_device = 0x644F7574
    prop_nominal_sample_rate = 0x6E737274
    scope_output = 0x6F7574
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
            address = _PropertyAddress(selector, scope_output, 0)
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

        device_id = _get_property(1, prop_default_output_device, 4, ctypes.c_uint32)
        if device_id:
            return _get_property(
                device_id, prop_nominal_sample_rate, 8, ctypes.c_double
            )
        return 0
    except OSError, AttributeError:
        return 0


def detect_output_rate() -> int:
    return nominal_output_rate(probe_output_rate())


def default_data_format() -> str:
    return f"LEI16@{detect_output_rate()}"


@dataclass(frozen=True)
class AudioLibrary:
    module: Any


def load_audio_library() -> AudioLibrary | None:
    try:
        import miniaudio
    except ImportError:
        return None
    return AudioLibrary(module=miniaudio)


def load_miniaudio(audio: AudioLibrary, path: Path) -> bytes:
    try:
        decoded = audio.module.wav_read_file_s16(str(path))
    except audio.module.MiniaudioError as exc:
        raise RuntimeError(str(exc)) from exc
    return cast(bytes, decoded.samples.tobytes())


def read_wave(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        return wav.readframes(wav.getnframes())


def load_failures() -> tuple[type[BaseException], ...]:
    return (OSError, EOFError, wave.Error, RuntimeError)


def failure_detail(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:200]


def load_wav(engine: StreamEngine, path: Path) -> bytes:
    if engine.audio is not None:
        return load_miniaudio(engine.audio, path)
    return read_wave(path)


@dataclass
class StreamCell:
    stream: StreamState
    closed: bool
    channels: int
    gap_size_bytes: int
    device: Any
    drain_thread: threading.Thread | None


@dataclass(frozen=True)
class StreamEngine:
    lock: threading.Lock
    events: queue.SimpleQueue[EngineEvent]
    chunk_bytes: int
    gap_ms: int
    fail_at: int
    delay: float
    log_path: str | None
    drain: bool
    audio: AudioLibrary | None
    cell: StreamCell
    load: Callable[[StreamEngine, Path], bytes] = load_wav


def make_stream_engine(
    *,
    load: Callable[[StreamEngine, Path], bytes] = load_wav,
    chunk_bytes: int = 1102,
    gap_ms: int = 0,
    fail_at: int = 0,
    rate: int = 22050,
    channels: int = 1,
    delay: float = 0.0,
    log_path: str | None = None,
    drain: bool = False,
    audio: AudioLibrary | None = None,
) -> StreamEngine:
    return StreamEngine(
        lock=threading.Lock(),
        events=queue.SimpleQueue(),
        chunk_bytes=chunk_bytes,
        gap_ms=gap_ms,
        fail_at=fail_at,
        delay=delay,
        log_path=log_path,
        drain=drain,
        audio=audio,
        cell=StreamCell(
            stream=StreamState(),
            closed=False,
            channels=channels,
            gap_size_bytes=gap_bytes(gap_ms, rate, channels),
            device=None,
            drain_thread=None,
        ),
        load=load,
    )


def set_stream_format(engine: StreamEngine, rate: int, channels: int) -> None:
    engine.cell.channels = channels
    engine.cell.gap_size_bytes = gap_bytes(engine.gap_ms, rate, channels)


def ensure_device(engine: StreamEngine, path: Path) -> None:
    audio = engine.audio
    if audio is None or engine.cell.device is not None:
        return
    with wave.open(str(path), "rb") as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
    set_stream_format(engine, rate, channels)
    module = audio.module
    try:
        device = module.PlaybackDevice(
            output_format=module.SampleFormat.SIGNED16,
            nchannels=channels,
            sample_rate=rate,
            buffersize_msec=60,
        )
        pull = pull_frames(engine)
        next(pull)
        device.start(pull)
    except module.MiniaudioError as exc:
        raise RuntimeError(str(exc)) from exc
    engine.cell.device = device


def close_device(engine: StreamEngine) -> None:
    device = engine.cell.device
    if device is not None:
        device.close()
        engine.cell.device = None


def log_stream_started(engine: StreamEngine, index: int) -> None:
    if engine.log_path is None:
        return
    with open(engine.log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"{engine.cell.stream.paths[index]}\n")


def next_chunk(engine: StreamEngine, want_bytes: int) -> bytes | None:
    with engine.lock:
        chunk, engine.cell.stream, step_events = stream_next_chunk(
            engine.cell.stream, want_bytes, engine.cell.gap_size_bytes, engine.fail_at
        )
        for event in step_events:
            match event:
                case StreamStarted():
                    log_stream_started(engine, event.index)
                case _:
                    engine.events.put(event)
    return chunk


def pull(engine: StreamEngine) -> Iterator[bytes]:
    while not engine.cell.closed:
        chunk = next_chunk(engine, engine.chunk_bytes)
        yield chunk if chunk is not None else b"\x00" * engine.chunk_bytes


def pull_frames(engine: StreamEngine) -> Generator[bytes, int | None]:
    frames = yield b""
    while not engine.cell.closed:
        want_bytes = frames_to_bytes(max(int(frames or 0), 1), engine.cell.channels)
        chunk = next_chunk(engine, want_bytes)
        if chunk is None:
            chunk = bytes(want_bytes)
        elif len(chunk) < want_bytes:
            chunk += bytes(want_bytes - len(chunk))
        frames = yield chunk


def drain_loop(engine: StreamEngine) -> None:
    for _ in pull(engine):
        if engine.delay:
            time.sleep(engine.delay)


def start_drain(engine: StreamEngine) -> None:
    cell = engine.cell
    if cell.drain_thread is not None and cell.drain_thread.is_alive():
        return
    cell.drain_thread = threading.Thread(target=drain_loop, args=(engine,), daemon=True)
    cell.drain_thread.start()


def stream_engine_play(engine: StreamEngine, index: int, path: Path) -> None:
    try:
        ensure_device(engine, path)
        data = engine.load(engine, path)
    except load_failures() as exc:
        engine.events.put(
            StreamCrashed(index, f"could not load {path.name}: {failure_detail(exc)}")
        )
        return
    with engine.lock:
        engine.cell.stream = stream_play(engine.cell.stream, index, path, data)
    if engine.drain:
        start_drain(engine)


def stream_engine_prime(engine: StreamEngine, index: int, path: Path) -> None:
    try:
        data = engine.load(engine, path)
    except load_failures():
        return
    with engine.lock:
        engine.cell.stream = stream_prime(engine.cell.stream, index, path, data)


def stream_engine_stop_stream(engine: StreamEngine) -> None:
    with engine.lock:
        engine.cell.stream = stream_stop(engine.cell.stream)


def stream_engine_current_index(engine: StreamEngine) -> int | None:
    with engine.lock:
        return engine.cell.stream.current_index


def stream_engine_close(engine: StreamEngine) -> None:
    with engine.lock:
        engine.cell.closed = True
        engine.cell.stream = stream_stop(engine.cell.stream)
    close_device(engine)


@dataclass
class ProcCell:
    proc: subprocess.Popen[bytes] | None
    current_index: int | None
    suppress_report: bool


@dataclass(frozen=True)
class ProcEngine:
    events: queue.SimpleQueue[EngineEvent]
    cmd: tuple[str, ...]
    cell: ProcCell


def make_proc_engine(play_cmd: Sequence[str]) -> ProcEngine:
    return ProcEngine(
        events=queue.SimpleQueue(),
        cmd=tuple(play_cmd),
        cell=ProcCell(proc=None, current_index=None, suppress_report=True),
    )


def proc_engine_play(engine: ProcEngine, index: int, path: Path) -> None:
    proc_engine_stop_stream(engine)
    proc = subprocess.Popen(
        (*engine.cmd, str(path)), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    engine.cell.proc = proc
    engine.cell.current_index = index
    engine.cell.suppress_report = False
    threading.Thread(target=watch_proc, args=(engine, index, proc), daemon=True).start()


def watch_proc(engine: ProcEngine, index: int, proc: subprocess.Popen[bytes]) -> None:
    exit_code = proc.wait()
    if engine.cell.suppress_report:
        return
    if exit_code == 0:
        engine.events.put(StreamFinished(index, False))
    else:
        stderr = proc.stderr.read() if proc.stderr is not None else b""
        tail = " ".join(stderr.decode("utf-8", "replace").split())[:200]
        engine.events.put(
            StreamCrashed(index, f"player exited with code {exit_code}: {tail}")
        )


def proc_engine_stop_stream(engine: ProcEngine) -> None:
    proc = engine.cell.proc
    if proc is not None and proc.poll() is None:
        engine.cell.suppress_report = True
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    engine.cell.current_index = None


def proc_engine_current_index(engine: ProcEngine) -> int | None:
    proc = engine.cell.proc
    if proc is None or engine.cell.current_index is None:
        return None
    return engine.cell.current_index if proc.poll() is None else None


def proc_engine_close(engine: ProcEngine) -> None:
    proc_engine_stop_stream(engine)


def make_test_engine(
    gap_ms: int = 0, env: Mapping[str, str] | None = None
) -> StreamEngine:
    env_vars = env if env is not None else {}
    return make_stream_engine(
        gap_ms=gap_ms,
        fail_at=int(env_vars.get("T2S_TEST_PLAY_FAIL_AT", "0") or 0),
        delay=float(env_vars.get("T2S_TEST_PLAY_DELAY", "0.05") or 0),
        log_path=env_vars.get("T2S_TEST_PLAY_LOG"),
        drain=True,
    )


type Engine = StreamEngine | ProcEngine


def engine_play(engine: Engine, index: int, path: Path) -> None:
    match engine:
        case StreamEngine():
            stream_engine_play(engine, index, path)
        case ProcEngine():
            proc_engine_play(engine, index, path)


def engine_prime(engine: Engine, index: int, path: Path) -> None:
    match engine:
        case StreamEngine():
            stream_engine_prime(engine, index, path)
        case ProcEngine():
            pass


def engine_stop_stream(engine: Engine) -> None:
    match engine:
        case StreamEngine():
            stream_engine_stop_stream(engine)
        case ProcEngine():
            proc_engine_stop_stream(engine)


def engine_current_index(engine: Engine) -> int | None:
    match engine:
        case StreamEngine():
            return stream_engine_current_index(engine)
        case ProcEngine():
            return proc_engine_current_index(engine)


def engine_close(engine: Engine) -> None:
    match engine:
        case StreamEngine():
            stream_engine_close(engine)
        case ProcEngine():
            proc_engine_close(engine)


def make_engine(
    player: str,
    play_cmd: Sequence[str],
    gap_ms: int = 0,
    env: Mapping[str, str] | None = None,
    audio: AudioLibrary | None = None,
) -> Engine:
    choice = engine_choice(player, audio is not None)
    if choice == "missing-miniaudio":
        print(
            "t2s: --player miniaudio but the 'miniaudio' package is not installed",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if choice == "afplay-fallback":
        print(
            "t2s: miniaudio is not installed - falling back to afplay "
            "(pip install miniaudio for gapless playback)",
            file=sys.stderr,
        )
    if choice == "miniaudio":
        return make_stream_engine(gap_ms=gap_ms, audio=audio)
    if choice == "test":
        return make_test_engine(gap_ms, env)
    return make_proc_engine(play_cmd)
