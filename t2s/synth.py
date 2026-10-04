from __future__ import annotations

import contextlib
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .pure import CacheFile, evictions, prefetch_window


def prune_cache(cache_dir: Path, limit_mb: float) -> None:
    if not cache_dir.is_dir():
        return
    for part in cache_dir.glob("*.part"):
        part.unlink(missing_ok=True)
    if limit_mb <= 0:
        return
    wav_files = (
        (path, path.stat()) for path in cache_dir.glob("*.wav") if path.is_file()
    )
    cache_files = [
        CacheFile(path, stat_info.st_size, stat_info.st_mtime)
        for path, stat_info in wav_files
    ]
    for path in evictions(cache_files, limit_mb * 1024 * 1024):
        path.unlink(missing_ok=True)


@dataclass
class WorkerCell:
    cond: threading.Condition
    cursor: int
    failed: dict[int, str]
    stop_flag: bool


@dataclass(frozen=True)
class SynthWorker:
    paragraphs: tuple[str, ...]
    keys: tuple[str, ...]
    cache_dir: Path
    say_cmd: tuple[str, ...]
    ahead: int
    cell: WorkerCell


def make_worker(
    paragraphs: Sequence[str],
    keys: Sequence[str],
    cache_dir: Path,
    say_cmd: Sequence[str],
    ahead: int = 3,
) -> SynthWorker:
    return SynthWorker(
        paragraphs=tuple(paragraphs),
        keys=tuple(keys),
        cache_dir=cache_dir,
        say_cmd=tuple(say_cmd),
        ahead=max(0, ahead),
        cell=WorkerCell(
            cond=threading.Condition(), cursor=0, failed={}, stop_flag=False
        ),
    )


def path_for(worker: SynthWorker, index: int) -> Path:
    return worker.cache_dir / f"{worker.keys[index]}.wav"


def set_cursor(worker: SynthWorker, index: int) -> None:
    with worker.cell.cond:
        if worker.cell.cursor != index:
            worker.cell.cursor = index
            worker.cell.cond.notify_all()


def clear_failure(worker: SynthWorker, index: int) -> None:
    with worker.cell.cond:
        if worker.cell.failed.pop(index, None) is not None:
            worker.cell.cond.notify_all()


@dataclass(frozen=True)
class SynthesisFailed:
    index: int
    detail: str


def ensure(worker: SynthWorker, index: int) -> Path | SynthesisFailed:
    with worker.cell.cond:
        while True:
            path = path_for(worker, index)
            if path.exists():
                return path
            if index in worker.cell.failed:
                return SynthesisFailed(index, worker.cell.failed[index])
            if worker.cell.stop_flag:
                return SynthesisFailed(index, "shutting down")
            worker.cell.cond.wait(0.1)


def stop_worker(worker: SynthWorker) -> None:
    with worker.cell.cond:
        worker.cell.stop_flag = True
        worker.cell.cond.notify_all()


def start_worker(worker: SynthWorker) -> None:
    threading.Thread(target=worker_run, args=(worker,), daemon=True).start()


def next_missing(worker: SynthWorker) -> int | None:
    for index in prefetch_window(worker.cell.cursor, worker.ahead, len(worker.keys)):
        if index not in worker.cell.failed and not path_for(worker, index).exists():
            return index
    return None


def worker_run(worker: SynthWorker) -> None:
    while True:
        with worker.cell.cond:
            if worker.cell.stop_flag:
                return
            target = next_missing(worker)
            if target is None:
                worker.cell.cond.wait(0.1)
                continue
        render(worker, target)


def render(worker: SynthWorker, index: int) -> None:
    path = path_for(worker, index)
    part_path = path.with_name(path.name + ".part")
    proc = subprocess.Popen(
        (*worker.say_cmd, "-o", str(part_path)),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    stderr_bytes = b""
    stdin = proc.stdin
    if stdin is not None:
        with contextlib.suppress(BrokenPipeError, OSError):
            stdin.write(worker.paragraphs[index].encode("utf-8"))
            stdin.close()
    stderr = proc.stderr
    if stderr is not None:
        with contextlib.suppress(OSError):
            stderr_bytes = stderr.read() or b""
    exit_code = proc.wait()
    succeeded = exit_code == 0 and part_path.exists()
    if succeeded:
        part_path.replace(path)
    with worker.cell.cond:
        if not succeeded:
            worker.cell.failed[index] = " ".join(
                stderr_bytes.decode("utf-8", "replace").split()
            )[:200]
        worker.cell.cond.notify_all()
