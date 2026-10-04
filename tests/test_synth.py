"""Unit tests for the synthesis cache and the prefetch worker."""

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from t2s import (
    CacheFile,
    SynthesisError,
    SynthWorker,
    cache_key,
    evictions,
    prune_cache,
)

FAKE_SAY = Path(__file__).resolve().parent / "fake_say.py"


def wait_until(
    pred: Callable[[], bool], timeout: float = 10.0, step: float = 0.02
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def make_worker(
    tmp_path: Path, paras: list[str], ahead: int = 3
) -> tuple[SynthWorker, Path]:
    keys = [cache_key(p, None, None) for p in paras]
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    worker = SynthWorker(
        paras, keys, cache, [sys.executable, str(FAKE_SAY)], ahead=ahead
    )
    worker.start()
    return worker, cache


def test_cache_key_fields() -> None:
    base = cache_key("hello world", None, None)
    assert cache_key("hello world", None, None) == base  # deterministic
    assert cache_key("hello world", "Fred", None) != base
    assert cache_key("hello world", None, 180) != base
    assert cache_key("hello  world", None, None) != base  # different text


def test_cache_key_depends_on_data_format() -> None:
    """The synthesis format is part of the key: cached audio of one format
    must never be served for a session pinned to another."""
    a = cache_key("hi", None, None, "LEI16@22050")
    b = cache_key("hi", None, None, "LEI16@48000")
    assert a != b


def test_evictions_oldest_first() -> None:
    """Pure budget policy: evict oldest until the total fits."""
    old = CacheFile(Path("old.wav"), 300, mtime=1.0)
    new = CacheFile(Path("new.wav"), 100, mtime=2.0)
    assert evictions([old, new], limit_bytes=200) == (Path("old.wav"),)
    assert evictions([old, new], limit_bytes=400) == ()  # already fits
    assert evictions([old, new], limit_bytes=50) == (Path("old.wav"), Path("new.wav"))
    assert evictions([], limit_bytes=100) == ()


def test_evictions_ignores_input_order() -> None:
    """mtime decides, not list position."""
    a = CacheFile(Path("a.wav"), 100, mtime=3.0)
    b = CacheFile(Path("b.wav"), 100, mtime=1.0)
    assert evictions([a, b], limit_bytes=150) == (Path("b.wav"),)


def test_prefetch_window(tmp_path: Path) -> None:
    paras = [f"paragraph number {i}" for i in range(8)]
    worker, _ = make_worker(tmp_path, paras, ahead=2)
    try:
        worker.set_cursor(0)
        assert worker.ensure(0).exists()  # current first
        assert wait_until(lambda: all(worker.path_for(i).exists() for i in (1, 2)))
        assert not worker.path_for(3).exists()  # outside cursor+ahead
        worker.set_cursor(5)  # jump: window re-centers
        assert wait_until(lambda: all(worker.path_for(i).exists() for i in (5, 6, 7)))
        assert not worker.path_for(4).exists()  # never entered any window
    finally:
        worker.stop()


def test_failure_then_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SAY_FAIL_TEXT", "jinx")
    paras = ["fine one", "jinxed paragraph", "fine two"]
    worker, _ = make_worker(tmp_path, paras, ahead=3)
    try:
        worker.set_cursor(0)
        assert worker.ensure(0).exists()
        with pytest.raises(SynthesisError):
            worker.ensure(1)  # render failed
        worker.clear_failure(1)  # user presses space
        monkeypatch.delenv("FAKE_SAY_FAIL_TEXT")
        assert worker.ensure(1).exists()  # retry succeeds
    finally:
        worker.stop()


def test_prune_evicts_oldest_and_removes_parts(tmp_path: Path) -> None:
    cache = tmp_path / "c"
    cache.mkdir()
    old = cache / "a.wav"
    new = cache / "b.wav"
    part = cache / "c.wav.part"
    old.write_bytes(b"x" * 300)
    new.write_bytes(b"y" * 100)
    part.write_bytes(b"z")
    past = time.time() - 10_000
    os.utime(old, (past, past))

    prune_cache(cache, limit_mb=200 / 1024 / 1024)  # 200-byte budget

    assert not old.exists()  # oldest, evicted
    assert new.exists()  # kept under the limit
    assert not part.exists()  # stale partial removed


def test_prune_zero_limit_keeps_everything(tmp_path: Path) -> None:
    cache = tmp_path / "c"
    cache.mkdir()
    (cache / "a.wav").write_bytes(b"x" * 5000)
    prune_cache(cache, limit_mb=0)  # 0 = unlimited
    assert (cache / "a.wav").exists()
