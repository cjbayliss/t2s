import argparse
import contextlib
import os
import queue
import select
import sys
import termios
import tty
from collections.abc import Callable
from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import final

from t2s.effects import path_exists, print_error, print_line
from t2s.engines import (
    AudioLibrary,
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
from t2s.errors import StartupError, SynthesisError
from t2s.monads import (
    IO,
    Failure,
    Reference,
    Result,
    Success,
    io,
    io_bind,
    io_map,
    io_pure,
    io_run,
    make_reference,
    read_reference,
    write_reference,
)
from t2s.pure import (
    AppState,
    ClearFailure,
    Effect,
    Effects,
    EngineChoice,
    EngineEvent,
    Note,
    PlayOutcome,
    StopStream,
    SyncTo,
    TryPlay,
    Warn,
    apply_play_outcome,
    cache_key,
    decode_chunk,
    engine_choice,
    gap_supported,
    handle_engine_event,
    handle_key,
    resolve_backend_binary,
    resolve_cache_directory,
    resolve_play_binary,
    say_command,
    synthesis_failure_message,
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


@final
@dataclass(frozen=True, slots=True)
class Arguments:
    file: str
    backend: str
    backend_options: tuple[str, ...]
    backend_binary: str | None
    width: int
    start: int
    split_long: int | None
    ahead: int
    cache_directory: str | None
    cache_limit_megabytes: float
    play_binary: str | None
    gap_milliseconds: int
    player: str


def arguments_from_namespace(namespace_arguments: argparse.Namespace) -> Arguments:
    return Arguments(
        file=namespace_arguments.file,
        backend=namespace_arguments.backend,
        backend_options=tuple(namespace_arguments.backend_options or ()),
        backend_binary=namespace_arguments.backend_binary,
        width=namespace_arguments.width,
        start=namespace_arguments.start,
        split_long=namespace_arguments.split_long,
        ahead=namespace_arguments.ahead,
        cache_directory=namespace_arguments.cache_directory,
        cache_limit_megabytes=namespace_arguments.cache_limit_megabytes,
        play_binary=namespace_arguments.play_binary,
        gap_milliseconds=namespace_arguments.gap,
        player=namespace_arguments.player,
    )


@final
@dataclass(frozen=True, slots=True)
class KeySource:
    file_descriptor: int
    restore: tuple[int | list[bytes | int], ...]
    close_file_descriptor: bool
    pending: Reference[bytes]


def open_key_source() -> IO[KeySource | None]:
    def opened() -> KeySource | None:
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
        pending = io_run(make_reference(b""))
        return KeySource(
            file_descriptor=file_descriptor,
            restore=restore,
            close_file_descriptor=close_file_descriptor,
            pending=pending,
        )

    return io(opened)


def engine_warnings(choice: EngineChoice, gap_milliseconds: int) -> tuple[str, ...]:
    fallback = (
        (
            "t2s: miniaudio is not installed - falling back to afplay "
            "(pip install miniaudio for gapless playback)",
        )
        if choice == "afplay-fallback"
        else ()
    )
    gap = (
        ("t2s: --gap is only supported by the miniaudio engine - ignoring",)
        if gap_milliseconds > 0 and not gap_supported(choice)
        else ()
    )
    return (*fallback, *gap)


def build_app(
    arguments: Arguments, paragraphs: tuple[str, ...], audio: AudioLibrary | None
) -> IO[Result[App, StartupError]]:
    def built() -> Result[App, StartupError]:
        environment = os.environ
        backend_binary = resolve_backend_binary(
            arguments.backend, arguments.backend_binary, environment
        )
        data_format = io_run(default_data_format())
        synthesis_command = say_command(
            backend_binary, arguments.backend_options, data_format
        )
        cache_keys = tuple(
            cache_key(paragraph, arguments.backend_options, data_format)
            for paragraph in paragraphs
        )
        cache_directory = resolve_cache_directory(
            arguments.cache_directory, Path.home()
        )
        cache_directory.mkdir(parents=True, exist_ok=True)
        io_run(prune_cache(cache_directory, arguments.cache_limit_megabytes))
        synthesis_worker = io_run(
            make_worker(
                paragraphs,
                cache_keys,
                cache_directory,
                synthesis_command,
                ahead=arguments.ahead,
            )
        )
        play_command = (resolve_play_binary(arguments.play_binary, environment),)
        engine = io_run(
            make_engine(
                arguments.player,
                play_command,
                arguments.gap_milliseconds,
                environment,
                audio=audio,
            )
        )
        return Success(
            App(
                paragraphs=paragraphs,
                start_index=arguments.start - 1,
                width=arguments.width,
                use_ansi=sys.stdout.isatty(),
                key_source=io_run(open_key_source()),
                synthesis_worker=synthesis_worker,
                engine=engine,
            )
        )

    return io(built)


def open_app(
    arguments: Arguments, paragraphs: tuple[str, ...]
) -> IO[Result[App, StartupError]]:
    return io_bind(load_audio_library(), choose_app(arguments, paragraphs))


def choose_app(
    arguments: Arguments, paragraphs: tuple[str, ...]
) -> Callable[[AudioLibrary | None], IO[Result[App, StartupError]]]:
    def continue_with(audio: AudioLibrary | None) -> IO[Result[App, StartupError]]:
        choice = engine_choice(arguments.player, audio is not None)
        if choice == "missing-miniaudio":
            error = StartupError(
                message=(
                    "t2s: --player miniaudio but the 'miniaudio' package "
                    "is not installed"
                ),
                exit_code=2,
            )
            return io_map(print_error(error.message), lambda _: Failure(error))
        warned = io_pure(None)
        warned = reduce(
            lambda pending_effect, warning: io_bind(
                pending_effect, lambda _: print_error(warning)
            ),
            engine_warnings(choice, arguments.gap_milliseconds),
            warned,
        )
        return io_bind(warned, lambda _: build_app(arguments, paragraphs, audio))

    return continue_with


@final
@dataclass(frozen=True, slots=True)
class App:
    paragraphs: tuple[str, ...]
    start_index: int
    width: int
    use_ansi: bool
    key_source: KeySource | None
    synthesis_worker: SynthesisWorker
    engine: Engine


def perform(app: App, state: AppState, effect: Effect) -> IO[tuple[AppState, Effects]]:
    match effect:
        case TryPlay(index, note):
            return io_map(
                try_play(app, index, note),
                lambda outcome: apply_play_outcome(state, index, outcome),
            )
        case StopStream():
            return io_map(engine_stop_stream(app.engine), lambda _: (state, ()))
        case ClearFailure(index):
            return io_map(
                clear_failure(app.synthesis_worker, index), lambda _: (state, ())
            )
        case SyncTo(index):
            return io_map(sync_to(app, index), lambda _: (state, ()))
        case Note(text):
            return io_map(emit_note(app, text), lambda _: (state, ()))
        case Warn(text):
            return io_map(print_error(text), lambda _: (state, ()))


def interpret(app: App, state: AppState, effects: Effects) -> IO[AppState]:
    def interpreted() -> AppState:
        current = state
        pending = effects
        while pending:
            effect = pending[0]
            next_state, produced = io_run(perform(app, current, effect))
            current = next_state
            pending = (*produced, *pending[1:])
        return current

    return io(interpreted)


def try_play(app: App, index: int, note_text: str | None = None) -> IO[PlayOutcome]:
    prepared = io_bind(
        engine_stop_stream(app.engine),
        lambda _: set_cursor(app.synthesis_worker, index),
    )
    shown = io_bind(prepared, lambda _: show_paragraph(app, index, note_text))
    return io_bind(shown, lambda _: decide_play(app, index))


def decide_play(app: App, index: int) -> IO[PlayOutcome]:
    return io_bind(ensure(app.synthesis_worker, index), start_or_pause(app, index))


def start_or_pause(
    app: App, index: int
) -> Callable[[Path | SynthesisFailed], IO[PlayOutcome]]:
    def continue_with(rendered: Path | SynthesisFailed) -> IO[PlayOutcome]:
        match rendered:
            case SynthesisFailed(error=error):
                return report_synthesis_failure(app, error)
            case Path() as path:
                outcome: PlayOutcome = "playing"
                return io_bind(
                    engine_play(app.engine, index, path),
                    lambda _: io_bind(
                        prime_next(app, index), lambda _: io_pure(outcome)
                    ),
                )

    return continue_with


def report_synthesis_failure(app: App, error: SynthesisError) -> IO[PlayOutcome]:
    message = f"! could not render paragraph: {synthesis_failure_message(error)}"
    if app.key_source is not None:
        outcome: PlayOutcome = "paused"
        warned = io_bind(
            print_error(message),
            lambda _: print_line("space: retry, n/p: paragraph, q: quit"),
        )
        return io_bind(warned, lambda _: io_pure(outcome))
    outcome = "failed"
    continued = print_error(message + " - continuing with next paragraph")
    return io_bind(continued, lambda _: io_pure(outcome))


def prime_next(app: App, index: int) -> IO[None]:
    next_index = index + 1
    if next_index >= len(app.paragraphs):
        return io_pure(None)
    next_path = path_for(app.synthesis_worker, next_index)
    return io_bind(
        path_exists(next_path),
        lambda exists: (
            engine_prime(app.engine, next_index, next_path) if exists else io_pure(None)
        ),
    )


def sync_to(app: App, index: int) -> IO[None]:
    aligned = io_bind(
        set_cursor(app.synthesis_worker, index),
        lambda _: show_paragraph(app, index),
    )
    return io_bind(aligned, lambda _: prime_next(app, index))


def run(app: App) -> IO[int]:
    def played() -> int:
        state = AppState(
            index=app.start_index,
            paragraph_count=len(app.paragraphs),
            interactive=app.key_source is not None,
        )
        io_run(start_worker(app.synthesis_worker))
        code = 0
        try:
            state = io_run(interpret(app, state, (TryPlay(state.index),)))
            while state.running:
                state = io_run(tick(app, state))
            code = 1 if state.had_errors else 0
        except KeyboardInterrupt:
            io_run(emit_note(app, "interrupted"))
            code = 130
        finally:
            io_run(restore_terminal(app))
            io_run(engine_stop_stream(app.engine))
            io_run(engine_close(app.engine))
            io_run(stop_worker(app.synthesis_worker))
            if app.use_ansi:
                sys.stdout.write("\x1b[0m\x1b[?25h")
                sys.stdout.flush()
        return code

    return io(played)


def restore_terminal(app: App) -> IO[None]:
    def restored() -> None:
        key_source = app.key_source
        if key_source is None:
            return
        with contextlib.suppress(termios.error):
            termios.tcsetattr(
                key_source.file_descriptor,
                termios.TCSADRAIN,
                list(key_source.restore),
            )
        if key_source.close_file_descriptor:
            with contextlib.suppress(OSError):
                os.close(key_source.file_descriptor)

    return io(restored)


def tick(app: App, state: AppState) -> IO[AppState]:
    return io_bind(poll_keys(app, state), lambda polled: drain_events(app, polled))


def wait_for_key(app: App) -> IO[bool]:
    def waited() -> bool:
        key_source = app.key_source
        file_descriptors = (
            [key_source.file_descriptor] if key_source is not None else []
        )
        try:
            ready, _, _ = select.select(file_descriptors, [], [], 0.05)
        except OSError, ValueError:
            return False
        return key_source is not None and key_source.file_descriptor in ready

    return io(waited)


def poll_keys(app: App, state: AppState) -> IO[AppState]:
    return io_bind(wait_for_key(app), read_ready_keys(app, state))


def read_ready_keys(app: App, state: AppState) -> Callable[[bool], IO[AppState]]:
    def continue_with(ready: bool) -> IO[AppState]:
        key_source = app.key_source
        if not ready or key_source is None:
            return io_pure(state)
        return io_bind(
            read_key_bytes(key_source),
            lambda data: append_key_bytes(app, key_source, state, data),
        )

    return continue_with


def read_key_bytes(key_source: KeySource) -> IO[bytes]:
    def read() -> bytes:
        try:
            return os.read(key_source.file_descriptor, 256)
        except OSError:
            return b""

    return io(read)


def append_key_bytes(
    app: App, key_source: KeySource, state: AppState, data: bytes
) -> IO[AppState]:
    return io_bind(
        read_reference(key_source.pending),
        lambda pending: decode_pending(app, key_source, state, pending, data),
    )


def decode_pending(
    app: App,
    key_source: KeySource,
    state: AppState,
    pending: bytes,
    data: bytes,
) -> IO[AppState]:
    text, hold = decode_chunk(pending, data)
    return io_bind(
        write_reference(key_source.pending, hold),
        lambda _: interpret_keys(app, state, tuple(text)),
    )


def interpret_keys(app: App, state: AppState, keys: tuple[str, ...]) -> IO[AppState]:
    def handled() -> AppState:
        current = state
        for key in keys:
            engine_index = io_run(engine_current_index(app.engine))
            next_state, effects = handle_key(current, key, engine_index)
            current = io_run(interpret(app, next_state, effects))
        return current

    return io(handled)


def next_event(app: App) -> IO[EngineEvent | None]:
    def fetched() -> EngineEvent | None:
        try:
            return app.engine.events.get_nowait()
        except queue.Empty:
            return None

    return io(fetched)


def drain_events(app: App, state: AppState) -> IO[AppState]:
    def drained() -> AppState:
        current = state
        while True:
            match io_run(next_event(app)):
                case None:
                    return current
                case event:
                    next_state, effects = handle_engine_event(current, event)
                    current = io_run(interpret(app, next_state, effects))

    return io(drained)


def show_paragraph(app: App, index: int, note_text: str | None = None) -> IO[None]:
    header = f"- {index + 1}/{len(app.paragraphs)}"
    if note_text:
        header += f" {note_text}"
    header += " -"
    lines = tuple(line for line, _ in wrap_offsets(app.paragraphs[index], app.width))
    return print_line("\n".join((dim(app, header), *lines)))


def dim(app: App, text: str) -> str:
    return f"\x1b[2m{text}\x1b[0m" if app.use_ansi else text


def emit_note(app: App, text: str) -> IO[None]:
    return print_line(dim(app, text))
