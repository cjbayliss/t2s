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
    wavs = ((p, p.stat()) for p in cache_dir.glob("*.wav") if p.is_file())
    files = [CacheFile(p, st.st_size, st.st_mtime) for p, st in wavs]
    for path in evictions(files, limit_mb * 1024 * 1024):
        path.unlink(missing_ok=True)


class SynthWorker(threading.Thread):
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
        self._say_cmd = say_cmd
        self.ahead = max(0, ahead)
        self._cond = threading.Condition()
        self._cursor = 0
        self._failed: dict[int, str] = {}
        self._stop_flag = False

    def path_for(self, idx: int) -> Path:
        return self.cache_dir / f"{self._keys[idx]}.wav"

    def set_cursor(self, idx: int) -> None:
        with self._cond:
            if self._cursor != idx:
                self._cursor = idx
                self._cond.notify_all()

    def clear_failure(self, idx: int) -> None:
        with self._cond:
            if self._failed.pop(idx, None) is not None:
                self._cond.notify_all()

    def ensure(self, idx: int) -> Path:
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
        for idx in prefetch_window(self._cursor, self.ahead, len(self._keys)):
            if idx not in self._failed and not self.path_for(idx).exists():
                return idx
        return None

    def _render(self, idx: int) -> None:
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
                stdin.close()
        stderr = proc.stderr
        if stderr is not None:
            with contextlib.suppress(OSError):
                err = stderr.read() or b""
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
