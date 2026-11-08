import contextlib
import queue
import subprocess
import threading
import wave
from array import array
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, final

from t2s.effects import sleep
from t2s.errors import (
    AudioDecodeError,
    AudioLoadError,
    PlayerExitError,
)
from t2s.monads import (
    IO,
    Reference,
    io,
    io_bind,
    io_map,
    io_pure,
    io_run,
    make_reference,
    modify_reference,
    read_reference,
    update_reference,
)
from t2s.pure import (
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

if TYPE_CHECKING:
    import miniaudio


type MiniaudioErrorType = type[miniaudio.MiniaudioError]
type MiniaudioSampleFormatType = type[miniaudio.SampleFormat]
type MiniaudioPlaybackDeviceType = type[miniaudio.PlaybackDevice]
type MiniaudioWavReader = Callable[[str], miniaudio.DecodedSoundFile]


class MiniaudioPlaybackDevice(Protocol):
    sample_rate: int

    def start(self, callback_generator: Generator[bytes | array[int], int]) -> None: ...

    def close(self) -> None: ...


class MiniaudioModule(Protocol):
    @property
    def MiniaudioError(self) -> MiniaudioErrorType: ...

    @property
    def SampleFormat(self) -> MiniaudioSampleFormatType: ...

    @property
    def PlaybackDevice(self) -> MiniaudioPlaybackDeviceType: ...

    @property
    def wav_read_file_s16(self) -> MiniaudioWavReader: ...


def probe_output_rate() -> IO[int]:
    def probed() -> int:
        property_default_output_device = 0x644F7574
        property_nominal_sample_rate = 0x6E737274
        scope_output = 0x6F7574
        try:
            import ctypes

            @final
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

            device_id = _get_property(
                1, property_default_output_device, 4, ctypes.c_uint32
            )
            if device_id:
                return _get_property(
                    device_id, property_nominal_sample_rate, 8, ctypes.c_double
                )
            return 0
        except OSError, AttributeError:
            return 0

    return io(probed)


def detect_output_rate() -> IO[int]:
    return io_map(probe_output_rate(), nominal_output_rate)


def default_data_format() -> IO[str]:
    return io_map(detect_output_rate(), lambda rate: f"LEI16@{rate}")


@final
@dataclass(frozen=True, slots=True)
class AudioLibrary:
    module: MiniaudioModule


def load_audio_library() -> IO[AudioLibrary | None]:
    def loaded() -> AudioLibrary | None:
        try:
            import miniaudio
        except ImportError:
            return None
        return AudioLibrary(module=miniaudio)

    return io(loaded)


def load_miniaudio(audio: AudioLibrary, path: Path) -> IO[bytes]:
    def decoded() -> bytes:
        try:
            wav = audio.module.wav_read_file_s16(str(path))
        except audio.module.MiniaudioError as exception:
            raise AudioDecodeError(str(exception)) from exception
        return cast(bytes, wav.samples.tobytes())

    return io(decoded)


def read_wave(path: Path) -> IO[bytes]:
    def read() -> bytes:
        with wave.open(str(path), "rb") as wav:
            return wav.readframes(wav.getnframes())

    return io(read)


def load_failures() -> tuple[type[BaseException], ...]:
    return (OSError, EOFError, wave.Error, RuntimeError, AudioDecodeError)


def audio_load_error(path: Path, exception: BaseException) -> AudioLoadError:
    return AudioLoadError(
        file_name=path.name,
        exception_type=type(exception).__name__,
        detail=str(exception),
    )


def load_wav(engine: StreamEngine, path: Path) -> IO[bytes]:
    audio = engine.audio
    if audio is None:
        return read_wave(path)
    return load_miniaudio(audio, path)


@final
@dataclass(frozen=True, slots=True)
class StreamCellState:
    stream: StreamState
    closed: bool
    channels: int
    gap_size_bytes: int
    device: MiniaudioPlaybackDevice | None
    drain_thread: threading.Thread | None


@final
@dataclass(frozen=True, slots=True)
class StreamEngine:
    events: queue.SimpleQueue[EngineEvent]
    chunk_bytes: int
    gap_milliseconds: int
    fail_at: int
    delay: float
    log_path: str | None
    drain: bool
    audio: AudioLibrary | None
    cell: Reference[StreamCellState]
    prime_async: bool = False
    load: Callable[[StreamEngine, Path], IO[bytes]] = load_wav


def make_stream_engine(
    *,
    load: Callable[[StreamEngine, Path], IO[bytes]] = load_wav,
    chunk_bytes: int = 1102,
    gap_milliseconds: int = 0,
    fail_at: int = 0,
    rate: int = 22050,
    channels: int = 1,
    delay: float = 0.0,
    log_path: str | None = None,
    drain: bool = False,
    audio: AudioLibrary | None = None,
    prime_async: bool = False,
) -> IO[StreamEngine]:
    initial_cell = StreamCellState(
        stream=StreamState(),
        closed=False,
        channels=channels,
        gap_size_bytes=gap_bytes(gap_milliseconds, rate, channels),
        device=None,
        drain_thread=None,
    )
    return io_bind(
        make_reference(initial_cell),
        lambda cell: io_pure(
            StreamEngine(
                events=queue.SimpleQueue(),
                chunk_bytes=chunk_bytes,
                gap_milliseconds=gap_milliseconds,
                fail_at=fail_at,
                delay=delay,
                log_path=log_path,
                drain=drain,
                audio=audio,
                cell=cell,
                prime_async=prime_async,
                load=load,
            )
        ),
    )


def set_stream_format(engine: StreamEngine, rate: int, channels: int) -> IO[None]:
    return modify_reference(
        engine.cell,
        lambda cell: replace(
            cell,
            channels=channels,
            gap_size_bytes=gap_bytes(engine.gap_milliseconds, rate, channels),
        ),
    )


def cell_closed(engine: StreamEngine) -> IO[bool]:
    return io_map(read_reference(engine.cell), lambda cell: cell.closed)


def ensure_device(engine: StreamEngine, path: Path) -> IO[None]:
    def ensured() -> None:
        audio = engine.audio
        cell = io_run(read_reference(engine.cell))
        if audio is None or cell.device is not None:
            return
        with wave.open(str(path), "rb") as wav:
            rate, channels = wav.getframerate(), wav.getnchannels()
        io_run(set_stream_format(engine, rate, channels))
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
        except module.MiniaudioError as exception:
            raise AudioDecodeError(str(exception)) from exception
        io_run(
            modify_reference(engine.cell, lambda state: replace(state, device=device))
        )

    return io(ensured)


def close_device(engine: StreamEngine) -> IO[None]:
    def closed() -> None:
        cell = io_run(read_reference(engine.cell))
        device = cell.device
        if device is not None:
            device.close()
            io_run(
                modify_reference(engine.cell, lambda state: replace(state, device=None))
            )

    return io(closed)


def log_stream_started(engine: StreamEngine, index: int) -> IO[None]:
    def logged() -> None:
        cell = io_run(read_reference(engine.cell))
        log_path = engine.log_path
        if log_path is not None:
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"{cell.stream.paths[index]}\n")

    return io(logged)


def step_stream(
    engine: StreamEngine, cell: StreamCellState, want_bytes: int
) -> tuple[tuple[bytes | None, tuple[EngineEvent, ...]], StreamCellState]:
    chunk, stream, events = stream_next_chunk(
        cell.stream, want_bytes, cell.gap_size_bytes, engine.fail_at
    )
    return (chunk, events), replace(cell, stream=stream)


def report_events(engine: StreamEngine, events: tuple[EngineEvent, ...]) -> IO[None]:
    def reported() -> None:
        for event in events:
            match event:
                case StreamStarted():
                    io_run(log_stream_started(engine, event.index))
                case _:
                    engine.events.put(event)

    return io(reported)


def next_chunk(engine: StreamEngine, want_bytes: int) -> IO[bytes | None]:
    stepped = update_reference(
        engine.cell, lambda cell: step_stream(engine, cell, want_bytes)
    )
    return io_bind(stepped, publish_stepped(engine))


def publish_stepped(
    engine: StreamEngine,
) -> Callable[[tuple[bytes | None, tuple[EngineEvent, ...]]], IO[bytes | None]]:
    def continue_with(
        stepped: tuple[bytes | None, tuple[EngineEvent, ...]],
    ) -> IO[bytes | None]:
        chunk, events = stepped
        return io_map(report_events(engine, events), lambda _: chunk)

    return continue_with


def pull(engine: StreamEngine) -> Iterator[bytes]:
    while not io_run(cell_closed(engine)):
        chunk = io_run(next_chunk(engine, engine.chunk_bytes))
        yield chunk if chunk is not None else b"\x00" * engine.chunk_bytes


def pull_frames(engine: StreamEngine) -> Generator[bytes, int | None]:
    frames = yield b""
    while not io_run(cell_closed(engine)):
        cell = io_run(read_reference(engine.cell))
        want_bytes = frames_to_bytes(max(int(frames or 0), 1), cell.channels)
        chunk = io_run(next_chunk(engine, want_bytes))
        if chunk is None:
            chunk = bytes(want_bytes)
        elif len(chunk) < want_bytes:
            chunk += bytes(want_bytes - len(chunk))
        frames = yield chunk


def drain_loop(engine: StreamEngine) -> None:
    for _ in pull(engine):
        if engine.delay:
            io_run(sleep(engine.delay))


def start_drain(engine: StreamEngine) -> IO[None]:
    def started() -> None:
        cell = io_run(read_reference(engine.cell))
        thread = cell.drain_thread
        if thread is not None and thread.is_alive():
            return
        spawned = threading.Thread(target=drain_loop, args=(engine,), daemon=True)
        io_run(
            modify_reference(
                engine.cell, lambda state: replace(state, drain_thread=spawned)
            )
        )
        spawned.start()

    return io(started)


def stream_engine_play(engine: StreamEngine, index: int, path: Path) -> IO[None]:
    def played() -> None:
        try:
            io_run(ensure_device(engine, path))
            data = io_run(engine.load(engine, path))
        except load_failures() as exception:
            engine.events.put(StreamCrashed(index, audio_load_error(path, exception)))
            return
        io_run(
            modify_reference(
                engine.cell,
                lambda cell: replace(
                    cell, stream=stream_play(cell.stream, index, path, data)
                ),
            )
        )
        if engine.drain:
            io_run(start_drain(engine))

    return io(played)


def prime_now(engine: StreamEngine, index: int, path: Path) -> IO[None]:
    def primed() -> None:
        try:
            data = io_run(engine.load(engine, path))
        except load_failures():
            return
        io_run(
            modify_reference(
                engine.cell,
                lambda cell: replace(
                    cell, stream=stream_prime(cell.stream, index, path, data)
                ),
            )
        )

    return io(primed)


def stream_engine_prime(engine: StreamEngine, index: int, path: Path) -> IO[None]:
    if not engine.prime_async:
        return prime_now(engine, index, path)

    def primed_async() -> None:
        cell = io_run(read_reference(engine.cell))
        if index in cell.stream.sources:
            return
        threading.Thread(
            target=lambda: io_run(prime_now(engine, index, path)), daemon=True
        ).start()

    return io(primed_async)


def stream_engine_stop_stream(engine: StreamEngine) -> IO[None]:
    return modify_reference(
        engine.cell, lambda cell: replace(cell, stream=stream_stop(cell.stream))
    )


def stream_engine_current_index(engine: StreamEngine) -> IO[int | None]:
    return io_map(read_reference(engine.cell), lambda cell: cell.stream.current_index)


def stream_engine_close(engine: StreamEngine) -> IO[None]:
    return io_bind(
        modify_reference(
            engine.cell,
            lambda cell: replace(cell, closed=True, stream=stream_stop(cell.stream)),
        ),
        lambda _: close_device(engine),
    )


@final
@dataclass(frozen=True, slots=True)
class ProcessState:
    process: subprocess.Popen[bytes] | None
    current_index: int | None
    suppress_report: bool


@final
@dataclass(frozen=True, slots=True)
class ProcessEngine:
    events: queue.SimpleQueue[EngineEvent]
    command: tuple[str, ...]
    state: Reference[ProcessState]


def make_process_engine(play_command: Sequence[str]) -> IO[ProcessEngine]:
    return io_bind(
        make_reference(
            ProcessState(process=None, current_index=None, suppress_report=True)
        ),
        lambda state: io_pure(
            ProcessEngine(
                events=queue.SimpleQueue(),
                command=tuple(play_command),
                state=state,
            )
        ),
    )


def process_engine_play(engine: ProcessEngine, index: int, path: Path) -> IO[None]:
    def played() -> None:
        io_run(process_engine_stop_stream(engine))
        process = subprocess.Popen(
            (*engine.command, str(path)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        io_run(
            modify_reference(
                engine.state,
                lambda state: replace(
                    state,
                    process=process,
                    current_index=index,
                    suppress_report=False,
                ),
            )
        )
        threading.Thread(
            target=watch_process, args=(engine, index, process), daemon=True
        ).start()

    return io(played)


def watch_process(
    engine: ProcessEngine, index: int, process: subprocess.Popen[bytes]
) -> None:
    stderr = process.stderr.read() if process.stderr is not None else b""
    exit_code = process.wait()
    state = io_run(read_reference(engine.state))
    if state.suppress_report:
        return
    match exit_code:
        case 0:
            engine.events.put(StreamFinished(index, False))
        case _:
            engine.events.put(
                StreamCrashed(
                    index,
                    PlayerExitError(
                        exit_code=exit_code,
                        standard_error=stderr.decode("utf-8", "replace"),
                    ),
                )
            )


def process_engine_stop_stream(engine: ProcessEngine) -> IO[None]:
    def stopped() -> None:
        state = io_run(read_reference(engine.state))
        process = state.process
        if process is not None and process.poll() is None:
            io_run(
                modify_reference(
                    engine.state,
                    lambda current: replace(current, suppress_report=True),
                )
            )
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        io_run(
            modify_reference(
                engine.state, lambda current: replace(current, current_index=None)
            )
        )

    return io(stopped)


def process_engine_current_index(engine: ProcessEngine) -> IO[int | None]:
    def queried() -> int | None:
        state = io_run(read_reference(engine.state))
        process = state.process
        if process is None or state.current_index is None:
            return None
        return state.current_index if process.poll() is None else None

    return io(queried)


def process_engine_close(engine: ProcessEngine) -> IO[None]:
    return process_engine_stop_stream(engine)


def make_test_engine(
    gap_milliseconds: int = 0, environment: Mapping[str, str] | None = None
) -> IO[StreamEngine]:
    environment_variables = environment if environment is not None else {}
    return make_stream_engine(
        gap_milliseconds=gap_milliseconds,
        fail_at=int(environment_variables.get("T2S_TEST_PLAY_FAIL_AT", "0") or 0),
        delay=float(environment_variables.get("T2S_TEST_PLAY_DELAY", "0.05") or 0),
        log_path=environment_variables.get("T2S_TEST_PLAY_LOG"),
        drain=True,
    )


type Engine = StreamEngine | ProcessEngine


def engine_play(engine: Engine, index: int, path: Path) -> IO[None]:
    match engine:
        case StreamEngine():
            return stream_engine_play(engine, index, path)
        case ProcessEngine():
            return process_engine_play(engine, index, path)


def engine_prime(engine: Engine, index: int, path: Path) -> IO[None]:
    match engine:
        case StreamEngine():
            return stream_engine_prime(engine, index, path)
        case ProcessEngine():
            return io_pure(None)


def engine_stop_stream(engine: Engine) -> IO[None]:
    match engine:
        case StreamEngine():
            return stream_engine_stop_stream(engine)
        case ProcessEngine():
            return process_engine_stop_stream(engine)


def engine_current_index(engine: Engine) -> IO[int | None]:
    match engine:
        case StreamEngine():
            return stream_engine_current_index(engine)
        case ProcessEngine():
            return process_engine_current_index(engine)


def engine_close(engine: Engine) -> IO[None]:
    match engine:
        case StreamEngine():
            return stream_engine_close(engine)
        case ProcessEngine():
            return process_engine_close(engine)


def make_engine(
    player: str,
    play_command: Sequence[str],
    gap_milliseconds: int = 0,
    environment: Mapping[str, str] | None = None,
    audio: AudioLibrary | None = None,
) -> IO[Engine]:
    def built() -> Engine:
        match engine_choice(player, audio is not None):
            case "miniaudio":
                return io_run(
                    make_stream_engine(
                        gap_milliseconds=gap_milliseconds,
                        audio=audio,
                        prime_async=True,
                    )
                )
            case "test":
                return io_run(make_test_engine(gap_milliseconds, environment))
            case _:
                return io_run(make_process_engine(play_command))

    return io(built)
