import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from t2s.pure import CacheFile, cache_key, evictions, prefetch_window
from t2s.synth import (
    SynthesisFailed,
    SynthWorker,
    clear_failure,
    ensure,
    make_worker,
    path_for,
    prune_cache,
    set_cursor,
    start_worker,
    stop_worker,
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


def spawn_worker(
    tmp_path: Path, paragraphs: list[str], ahead: int = 3
) -> tuple[SynthWorker, Path]:
    cache_keys = [
        cache_key(paragraph, None, None, "LEI16@22050") for paragraph in paragraphs
    ]
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    worker = make_worker(
        paragraphs, cache_keys, cache, [sys.executable, str(FAKE_SAY)], ahead=ahead
    )
    start_worker(worker)
    return worker, cache


def rendered_path(worker: SynthWorker, index: int) -> Path:
    rendered = ensure(worker, index)
    assert isinstance(rendered, Path)
    return rendered


def test_cache_key_fields() -> None:
    base = cache_key("hello world", None, None, "LEI16@22050")
    assert cache_key("hello world", None, None, "LEI16@22050") == base
    assert cache_key("hello world", "Fred", None, "LEI16@22050") != base
    assert cache_key("hello world", None, 180, "LEI16@22050") != base
    assert cache_key("hello  world", None, None, "LEI16@22050") != base


def test_cache_key_depends_on_data_format() -> None:
    a = cache_key("hi", None, None, "LEI16@22050")
    b = cache_key("hi", None, None, "LEI16@48000")
    assert a != b


def test_evictions_oldest_first() -> None:
    old = CacheFile(Path("old.wav"), 300, mtime=1.0)
    new = CacheFile(Path("new.wav"), 100, mtime=2.0)
    assert evictions([old, new], limit_bytes=200) == (Path("old.wav"),)
    assert evictions([old, new], limit_bytes=400) == ()
    assert evictions([old, new], limit_bytes=50) == (Path("old.wav"), Path("new.wav"))
    assert evictions([], limit_bytes=100) == ()


def test_evictions_ignores_input_order() -> None:
    a = CacheFile(Path("a.wav"), 100, mtime=3.0)
    b = CacheFile(Path("b.wav"), 100, mtime=1.0)
    assert evictions([a, b], limit_bytes=150) == (Path("b.wav"),)


def test_prefetch_window_is_cursor_to_cursor_plus_ahead() -> None:
    assert prefetch_window(0, 2, 8) == (0, 1, 2)
    assert prefetch_window(5, 2, 8) == (5, 6, 7)
    assert prefetch_window(7, 2, 8) == (7,)
    assert prefetch_window(6, 2, 8) == (6, 7)


def test_prefetch_window_edges() -> None:
    assert prefetch_window(0, 0, 3) == (0,)
    assert prefetch_window(0, 3, 0) == ()
    assert prefetch_window(0, -1, 3) == ()


def test_prefetch_window(tmp_path: Path) -> None:
    paragraphs = [f"paragraph number {i}" for i in range(8)]
    worker, _ = spawn_worker(tmp_path, paragraphs, ahead=2)
    try:
        set_cursor(worker, 0)
        assert rendered_path(worker, 0).exists()
        assert wait_until(lambda: all(path_for(worker, i).exists() for i in (1, 2)))
        assert not path_for(worker, 3).exists()
        set_cursor(worker, 5)
        assert wait_until(lambda: all(path_for(worker, i).exists() for i in (5, 6, 7)))
        assert not path_for(worker, 4).exists()
    finally:
        stop_worker(worker)


def test_failure_then_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SAY_FAIL_TEXT", "jinx")
    paragraphs = ["fine one", "jinxed paragraph", "fine two"]
    worker, _ = spawn_worker(tmp_path, paragraphs, ahead=3)
    try:
        set_cursor(worker, 0)
        assert rendered_path(worker, 0).exists()
        failed = ensure(worker, 1)
        assert isinstance(failed, SynthesisFailed)
        clear_failure(worker, 1)
        monkeypatch.delenv("FAKE_SAY_FAIL_TEXT")
        assert rendered_path(worker, 1).exists()
    finally:
        stop_worker(worker)


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

    prune_cache(cache, limit_mb=200 / 1024 / 1024)

    assert not old.exists()
    assert new.exists()
    assert not part.exists()


def test_prune_zero_limit_keeps_everything(tmp_path: Path) -> None:
    cache = tmp_path / "c"
    cache.mkdir()
    (cache / "a.wav").write_bytes(b"x" * 5000)
    prune_cache(cache, limit_mb=0)
    assert (cache / "a.wav").exists()
