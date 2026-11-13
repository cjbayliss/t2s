import os
import sys
import time
from pathlib import Path

import pytest
from conftest import wait_until

from t2s.monads import io_run
from t2s.pure import cache_key, prefetch_window
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

FAKE_SAY = Path(__file__).resolve().parent / "fake_say.py"


def spawn_worker(
    tmp_path: Path, paragraphs: list[str], ahead: int = 3
) -> tuple[SynthesisWorker, Path]:
    cache_keys = [cache_key(paragraph, (), "LEI16@22050") for paragraph in paragraphs]
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    worker = io_run(
        make_worker(
            paragraphs,
            cache_keys,
            cache,
            [sys.executable, str(FAKE_SAY)],
            ahead=ahead,
        )
    )
    io_run(start_worker(worker))
    return worker, cache


def rendered_path(worker: SynthesisWorker, index: int) -> Path:
    rendered = io_run(ensure(worker, index))
    assert isinstance(rendered, Path)
    return rendered


def test_prefetch_window_spans_cursor_through_cursor_plus_ahead() -> None:
    assert prefetch_window(0, 2, 8) == (0, 1, 2)
    assert prefetch_window(5, 2, 8) == (5, 6, 7)
    assert prefetch_window(7, 2, 8) == (7,)
    assert prefetch_window(6, 2, 8) == (6, 7)
    assert prefetch_window(0, 0, 3) == (0,)
    assert prefetch_window(0, 3, 0) == ()
    assert prefetch_window(0, -1, 3) == ()


def test_worker_prefetches_only_the_window(tmp_path: Path) -> None:
    paragraphs = [f"paragraph number {index}" for index in range(8)]
    worker, _ = spawn_worker(tmp_path, paragraphs, ahead=2)
    try:
        io_run(set_cursor(worker, 0))
        assert rendered_path(worker, 0).exists()
        assert wait_until(
            lambda: all(path_for(worker, index).exists() for index in (1, 2))
        )
        assert not path_for(worker, 3).exists()
        io_run(set_cursor(worker, 5))
        assert wait_until(
            lambda: all(path_for(worker, index).exists() for index in (5, 6, 7))
        )
        assert not path_for(worker, 4).exists()
    finally:
        io_run(stop_worker(worker))


def test_failure_then_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_SAY_FAIL_TEXT", "jinx")
    paragraphs = ["fine one", "jinxed paragraph", "fine two"]
    worker, _ = spawn_worker(tmp_path, paragraphs, ahead=3)
    try:
        io_run(set_cursor(worker, 0))
        assert rendered_path(worker, 0).exists()
        failed = io_run(ensure(worker, 1))
        assert isinstance(failed, SynthesisFailed)
        io_run(clear_failure(worker, 1))
        monkeypatch.delenv("FAKE_SAY_FAIL_TEXT")
        assert rendered_path(worker, 1).exists()
    finally:
        io_run(stop_worker(worker))


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

    io_run(prune_cache(cache, limit_megabytes=200 / 1024 / 1024))

    assert not old.exists()
    assert new.exists()
    assert not part.exists()


def test_prune_zero_limit_keeps_everything(tmp_path: Path) -> None:
    cache = tmp_path / "c"
    cache.mkdir()
    (cache / "a.wav").write_bytes(b"x" * 5000)
    io_run(prune_cache(cache, limit_megabytes=0))
    assert (cache / "a.wav").exists()
