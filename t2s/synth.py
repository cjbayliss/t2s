from __future__ import annotations

import contextlib
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

from .pure import CacheFile, evictions, prefetch_window


class SynthesisError(Exception):
    def __init__(self, index: int, detail: str) -> None:
        super().__init__(f"paragraph {index + 1}: {detail}")
        self.index = index
        self.detail = detail


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


class SynthWorker(threading.Thread):
    def __init__(
        self,
        paragraphs: Sequence[str],
        keys: Sequence[str],
        cache_dir: Path,
        say_cmd: Sequence[str],
        ahead: int = 3,
    ) -> None:
        super().__init__(daemon=True)
        self._paragraphs = paragraphs
        self._keys = keys
        self.cache_dir = cache_dir
        self._say_cmd = say_cmd
        self.ahead = max(0, ahead)
        self._cond = threading.Condition()
        self._cursor = 0
        self._failed: dict[int, str] = {}
        self._stop_flag = False

    def path_for(self, index: int) -> Path:
        return self.cache_dir / f"{self._keys[index]}.wav"

    def set_cursor(self, index: int) -> None:
        with self._cond:
            if self._cursor != index:
                self._cursor = index
                self._cond.notify_all()

    def clear_failure(self, index: int) -> None:
        with self._cond:
            if self._failed.pop(index, None) is not None:
                self._cond.notify_all()

    def ensure(self, index: int) -> Path:
        with self._cond:
            while True:
                path = self.path_for(index)
                if path.exists():
                    return path
                if index in self._failed:
                    raise SynthesisError(index, self._failed[index])
                if self._stop_flag:
                    raise SynthesisError(index, "shutting down")
                self._cond.wait(0.1)

    def stop(self) -> None:
        with self._cond:
            self._stop_flag = True
            self._cond.notify_all()

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
        for index in prefetch_window(self._cursor, self.ahead, len(self._keys)):
            if index not in self._failed and not self.path_for(index).exists():
                return index
        return None

    def _render(self, index: int) -> None:
        path = self.path_for(index)
        part_path = path.with_name(path.name + ".part")
        proc = subprocess.Popen(
            (*self._say_cmd, "-o", str(part_path)),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        stderr_bytes = b""
        stdin = proc.stdin
        if stdin is not None:
            with contextlib.suppress(BrokenPipeError, OSError):
                stdin.write(self._paragraphs[index].encode("utf-8"))
                stdin.close()
        stderr = proc.stderr
        if stderr is not None:
            with contextlib.suppress(OSError):
                stderr_bytes = stderr.read() or b""
        exit_code = proc.wait()
        succeeded = exit_code == 0 and part_path.exists()
        if succeeded:
            part_path.replace(path)
        with self._cond:
            if not succeeded:
                self._failed[index] = " ".join(
                    stderr_bytes.decode("utf-8", "replace").split()
                )[:200]
            self._cond.notify_all()
