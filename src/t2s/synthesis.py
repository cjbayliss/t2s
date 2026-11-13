import contextlib
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import final

from t2s.effects import sleep
from t2s.errors import (
    PhononSynthesisError,
    SaySynthesisError,
    SynthesisError,
    SynthesisShutdown,
)
from t2s.monads import (
    IO,
    Reference,
    io,
    io_bind,
    io_pure,
    io_run,
    make_reference,
    modify_reference,
    read_reference,
)
from t2s.phonon import PhononPlan, phonon_render
from t2s.pure import CacheFile, evictions, prefetch_window


def prune_cache(cache_directory: Path, limit_megabytes: float) -> IO[None]:
    def pruned() -> None:
        if not cache_directory.is_dir():
            return
        for part in cache_directory.glob("*.part"):
            part.unlink(missing_ok=True)
        if limit_megabytes <= 0:
            return
        wav_files = (
            (path, path.stat())
            for path in cache_directory.glob("*.wav")
            if path.is_file()
        )
        cache_files = [
            CacheFile(path, stat_result.st_size, stat_result.st_mtime)
            for path, stat_result in wav_files
        ]
        for path in evictions(cache_files, limit_megabytes * 1024 * 1024):
            path.unlink(missing_ok=True)

    return io(pruned)


@final
@dataclass(frozen=True, slots=True)
class WorkerState:
    cursor: int
    failed: Mapping[int, SynthesisError]
    stop_requested: bool


@final
@dataclass(frozen=True, slots=True)
class SayPlan:
    command: tuple[str, ...]


type SynthesisPlan = SayPlan | PhononPlan


@final
@dataclass(frozen=True, slots=True)
class SynthesisWorker:
    paragraphs: tuple[str, ...]
    keys: tuple[str, ...]
    cache_directory: Path
    synthesis_plan: SynthesisPlan
    ahead: int
    state: Reference[WorkerState]


def make_worker(
    paragraphs: Sequence[str],
    keys: Sequence[str],
    cache_directory: Path,
    synthesis_plan: SynthesisPlan,
    ahead: int = 3,
) -> IO[SynthesisWorker]:
    return io_bind(
        make_reference(WorkerState(cursor=0, failed={}, stop_requested=False)),
        lambda state: io_pure(
            SynthesisWorker(
                paragraphs=tuple(paragraphs),
                keys=tuple(keys),
                cache_directory=cache_directory,
                synthesis_plan=synthesis_plan,
                ahead=max(0, ahead),
                state=state,
            )
        ),
    )


def path_for(worker: SynthesisWorker, index: int) -> Path:
    return worker.cache_directory / f"{worker.keys[index]}.wav"


def set_cursor(worker: SynthesisWorker, index: int) -> IO[None]:
    return modify_reference(
        worker.state,
        lambda state: replace(state, cursor=index) if state.cursor != index else state,
    )


def clear_failure(worker: SynthesisWorker, index: int) -> IO[None]:
    return modify_reference(
        worker.state,
        lambda state: replace(
            state,
            failed={
                failed_index: error
                for failed_index, error in state.failed.items()
                if failed_index != index
            },
        ),
    )


@final
@dataclass(frozen=True, slots=True)
class SynthesisFailed:
    index: int
    error: SynthesisError


def ensure(worker: SynthesisWorker, index: int) -> IO[Path | SynthesisFailed]:
    def awaited() -> Path | SynthesisFailed:
        while True:
            path = path_for(worker, index)
            if path.exists():
                return path
            state = io_run(read_reference(worker.state))
            failed_error = state.failed.get(index)
            if failed_error is not None:
                return SynthesisFailed(index, failed_error)
            if state.stop_requested:
                return SynthesisFailed(index, SynthesisShutdown())
            io_run(sleep(0.1))

    return io(awaited)


def stop_worker(worker: SynthesisWorker) -> IO[None]:
    return modify_reference(
        worker.state, lambda state: replace(state, stop_requested=True)
    )


def start_worker(worker: SynthesisWorker) -> IO[None]:
    def started() -> None:
        threading.Thread(target=run_worker, args=(worker,), daemon=True).start()

    return io(started)


def missing_candidates(worker: SynthesisWorker, state: WorkerState) -> tuple[int, ...]:
    return tuple(
        index
        for index in prefetch_window(state.cursor, worker.ahead, len(worker.keys))
        if index not in state.failed
    )


def run_worker(worker: SynthesisWorker) -> None:
    while True:
        state = io_run(read_reference(worker.state))
        if state.stop_requested:
            return
        candidates = missing_candidates(worker, state)
        target = next(
            (index for index in candidates if not path_for(worker, index).exists()),
            None,
        )
        match target:
            case None:
                io_run(sleep(0.1))
            case _:
                io_run(render(worker, target))


def record_failure(
    worker: SynthesisWorker, index: int, error: SynthesisError
) -> IO[None]:
    return modify_reference(
        worker.state,
        lambda state: replace(state, failed={**state.failed, index: error}),
    )


def run_say(
    command: Sequence[str], paragraph: str, partial_file_path: Path
) -> SynthesisError | None:
    process = subprocess.Popen(
        (*command, "-o", str(partial_file_path)),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    stderr_bytes = b""
    stdin = process.stdin
    if stdin is not None:
        with contextlib.suppress(BrokenPipeError, OSError):
            stdin.write(paragraph.encode("utf-8"))
            stdin.close()
    stderr = process.stderr
    if stderr is not None:
        with contextlib.suppress(OSError):
            stderr_bytes = stderr.read() or b""
    exit_code = process.wait()
    if exit_code == 0 and partial_file_path.exists():
        return None
    return SaySynthesisError(
        exit_code=exit_code,
        standard_error=stderr_bytes.decode("utf-8", "replace"),
    )


def run_phonon(
    plan: PhononPlan, paragraph: str, partial_file_path: Path
) -> SynthesisError | None:
    try:
        io_run(phonon_render(plan, paragraph, partial_file_path))
    except PhononSynthesisError as error:
        return error
    return None


def render(worker: SynthesisWorker, index: int) -> IO[None]:
    def rendered() -> None:
        path = path_for(worker, index)
        partial_file_path = path.with_name(path.name + ".part")
        error: SynthesisError | None
        match worker.synthesis_plan:
            case SayPlan(command=command):
                error = run_say(command, worker.paragraphs[index], partial_file_path)
            case PhononPlan() as plan:
                error = run_phonon(plan, worker.paragraphs[index], partial_file_path)
        if error is None:
            partial_file_path.replace(path)
        else:
            io_run(record_failure(worker, index, error))

    return io(rendered)
