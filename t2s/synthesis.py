from __future__ import annotations

import contextlib
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from t2s.pure import CacheFile, evictions, prefetch_window


def prune_cache(cache_directory: Path, limit_megabytes: float) -> None:
    if not cache_directory.is_dir():
        return
    for part in cache_directory.glob("*.part"):
        part.unlink(missing_ok=True)
    if limit_megabytes <= 0:
        return
    wav_files = (
        (path, path.stat()) for path in cache_directory.glob("*.wav") if path.is_file()
    )
    cache_files = [
        CacheFile(path, stat_result.st_size, stat_result.st_mtime)
        for path, stat_result in wav_files
    ]
    for path in evictions(cache_files, limit_megabytes * 1024 * 1024):
        path.unlink(missing_ok=True)


@dataclass
class WorkerCell:
    condition: threading.Condition
    cursor: int
    failed: dict[int, str]
    stop_flag: bool


@dataclass(frozen=True)
class SynthesisWorker:
    paragraphs: tuple[str, ...]
    keys: tuple[str, ...]
    cache_directory: Path
    say_command: tuple[str, ...]
    ahead: int
    cell: WorkerCell


def make_worker(
    paragraphs: Sequence[str],
    keys: Sequence[str],
    cache_directory: Path,
    say_command: Sequence[str],
    ahead: int = 3,
) -> SynthesisWorker:
    return SynthesisWorker(
        paragraphs=tuple(paragraphs),
        keys=tuple(keys),
        cache_directory=cache_directory,
        say_command=tuple(say_command),
        ahead=max(0, ahead),
        cell=WorkerCell(
            condition=threading.Condition(), cursor=0, failed={}, stop_flag=False
        ),
    )


def path_for(worker: SynthesisWorker, index: int) -> Path:
    return worker.cache_directory / f"{worker.keys[index]}.wav"


def set_cursor(worker: SynthesisWorker, index: int) -> None:
    with worker.cell.condition:
        if worker.cell.cursor != index:
            worker.cell.cursor = index
            worker.cell.condition.notify_all()


def clear_failure(worker: SynthesisWorker, index: int) -> None:
    with worker.cell.condition:
        if worker.cell.failed.pop(index, None) is not None:
            worker.cell.condition.notify_all()


@dataclass(frozen=True)
class SynthesisFailed:
    index: int
    detail: str


def ensure(worker: SynthesisWorker, index: int) -> Path | SynthesisFailed:
    with worker.cell.condition:
        while True:
            path = path_for(worker, index)
            if path.exists():
                return path
            if index in worker.cell.failed:
                return SynthesisFailed(index, worker.cell.failed[index])
            if worker.cell.stop_flag:
                return SynthesisFailed(index, "shutting down")
            worker.cell.condition.wait(0.1)


def stop_worker(worker: SynthesisWorker) -> None:
    with worker.cell.condition:
        worker.cell.stop_flag = True
        worker.cell.condition.notify_all()


def start_worker(worker: SynthesisWorker) -> None:
    threading.Thread(target=worker_run, args=(worker,), daemon=True).start()


def next_missing(worker: SynthesisWorker) -> int | None:
    for index in prefetch_window(worker.cell.cursor, worker.ahead, len(worker.keys)):
        if index not in worker.cell.failed and not path_for(worker, index).exists():
            return index
    return None


def worker_run(worker: SynthesisWorker) -> None:
    while True:
        with worker.cell.condition:
            if worker.cell.stop_flag:
                return
            target = next_missing(worker)
            if target is None:
                worker.cell.condition.wait(0.1)
                continue
        render(worker, target)


def render(worker: SynthesisWorker, index: int) -> None:
    path = path_for(worker, index)
    partial_file_path = path.with_name(path.name + ".part")
    process = subprocess.Popen(
        (*worker.say_command, "-o", str(partial_file_path)),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    stderr_bytes = b""
    stdin = process.stdin
    if stdin is not None:
        with contextlib.suppress(BrokenPipeError, OSError):
            stdin.write(worker.paragraphs[index].encode("utf-8"))
            stdin.close()
    stderr = process.stderr
    if stderr is not None:
        with contextlib.suppress(OSError):
            stderr_bytes = stderr.read() or b""
    exit_code = process.wait()
    succeeded = exit_code == 0 and partial_file_path.exists()
    if succeeded:
        partial_file_path.replace(path)
    with worker.cell.condition:
        if not succeeded:
            worker.cell.failed[index] = " ".join(
                stderr_bytes.decode("utf-8", "replace").split()
            )[:200]
        worker.cell.condition.notify_all()
