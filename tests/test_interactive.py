import os
import pty
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import REPO, Fakes, engine_log_env, texts_played

pytestmark = pytest.mark.skipif(
    not hasattr(pty, "openpty"), reason="requires pty support"
)


def wait_until(
    pred: Callable[[], bool], timeout: float = 8.0, step: float = 0.05
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def spawn(
    fakes: Fakes,
    tmp_path: Path,
    doc: Path,
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], int, int, Path]:
    master, slave = pty.openpty()
    out_r, out_w = os.pipe()
    err_path = tmp_path / "stderr.log"
    env = os.environ.copy()
    env.update(env_extra or {})
    with open(err_path, "wb") as err_f:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "t2s",
                "--say-bin",
                fakes.say_bin,
                "--player",
                "test",
                "--cache-dir",
                str(tmp_path / "cache"),
                str(doc),
            ],
            stdin=slave,
            stdout=out_w,
            stderr=err_f,
            env=env,
            close_fds=True,
            cwd=REPO,
        )
    os.close(slave)
    os.close(out_w)
    return proc, master, out_r, err_path


def read_loop(fd: int, buf: bytearray) -> None:
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)


def cleanup(
    proc: subprocess.Popen[bytes], master: int, out_r: int, thread: threading.Thread
) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    os.close(master)
    os.close(out_r)
    thread.join(timeout=2)


def test_space_pauses_replays_and_q_quits(fakes: Fakes, tmp_path: Path) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text("one two three four five six seven eight nine ten eleven twelve")
    env, play_log = engine_log_env(tmp_path, delay="0.3")

    proc, master, out_r, _ = spawn(fakes, tmp_path, doc, env)
    buf = bytearray()
    t = threading.Thread(target=read_loop, args=(out_r, buf), daemon=True)
    t.start()

    try:
        assert wait_until(lambda: "¶ 1/1".encode() in buf), bytes(buf)
        assert wait_until(
            lambda: play_log.exists() and len(play_log.read_text().splitlines()) == 1
        ), "stream started"

        os.write(master, b" ")
        assert wait_until(lambda: b"paused" in buf), bytes(buf)
        time.sleep(0.7)
        lines = play_log.read_text().splitlines()
        assert len(lines) == 1

        os.write(master, b" ")
        assert wait_until(lambda: len(play_log.read_text().splitlines()) == 2), (
            "replayed"
        )
        lines = play_log.read_text().splitlines()
        assert lines[1] == lines[0]
        assert b"resumed" in bytes(buf)

        os.write(master, b"q")
        assert proc.wait(timeout=10) == 0
        assert "stopped at ¶ 1/1".encode() in bytes(buf)
    finally:
        cleanup(proc, master, out_r, t)


def test_chained_advance_without_keys(fakes: Fakes, tmp_path: Path) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text("First paragraph words.\n\nSecond paragraph words.")
    env, play_log = engine_log_env(tmp_path, delay="0.05")

    proc, master, out_r, _ = spawn(fakes, tmp_path, doc, env)
    buf = bytearray()
    t = threading.Thread(target=read_loop, args=(out_r, buf), daemon=True)
    t.start()

    try:
        assert wait_until(lambda: "¶ 1/2".encode() in buf), bytes(buf)
        assert wait_until(lambda: "¶ 2/2".encode() in bytes(buf)), bytes(buf)
        assert wait_until(
            lambda: play_log.exists() and len(play_log.read_text().splitlines()) == 2
        ), "both paragraphs streamed"
        os.write(master, b"q")
        rc = proc.wait(timeout=10)
        assert rc == 0, rc
        assert texts_played(fakes.say_log, play_log) == [
            "First paragraph words.",
            "Second paragraph words.",
        ]
    except OSError:
        pass
    finally:
        cleanup(proc, master, out_r, t)


def test_playback_error_waits_for_space(fakes: Fakes, tmp_path: Path) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text("only paragraph")

    env, play_log = engine_log_env(tmp_path, delay="0.3", fail_at="1")
    proc, master, out_r, err_path = spawn(fakes, tmp_path, doc, env)
    buf = bytearray()
    t = threading.Thread(target=read_loop, args=(out_r, buf), daemon=True)
    t.start()

    try:
        assert wait_until(lambda: b"device error" in buf), bytes(buf)
        err = err_path.read_text()
        assert "playback failed" in err
        os.write(master, b" ")
        assert wait_until(
            lambda: play_log.exists() and len(play_log.read_text().splitlines()) == 1
        ), "replayed after error"
        os.write(master, b"q")
        assert proc.wait(timeout=10) == 0
    finally:
        cleanup(proc, master, out_r, t)
