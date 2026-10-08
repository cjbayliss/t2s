import os
import pty
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import REPO, Fakes, engine_log_environment, texts_played

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
    environment_extra: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], int, int, Path]:
    master, slave = pty.openpty()
    output_read_file_descriptor, output_write_file_descriptor = os.pipe()
    error_log_path = tmp_path / "stderr.log"
    environment = os.environ.copy()
    environment.update(environment_extra or {})
    with open(error_log_path, "wb") as error_file:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "t2s",
                "--say-bin",
                fakes.say_binary,
                "--player",
                "test",
                "--cache-dir",
                str(tmp_path / "cache"),
                str(doc),
            ],
            stdin=slave,
            stdout=output_write_file_descriptor,
            stderr=error_file,
            env=environment,
            close_fds=True,
            cwd=REPO,
        )
    os.close(slave)
    os.close(output_write_file_descriptor)
    return process, master, output_read_file_descriptor, error_log_path


def read_loop(file_descriptor: int, buffer: bytearray) -> None:
    while True:
        try:
            chunk = os.read(file_descriptor, 4096)
        except OSError:
            break
        if not chunk:
            break
        buffer.extend(chunk)


def cleanup(
    process: subprocess.Popen[bytes],
    master: int,
    output_read_file_descriptor: int,
    reader: threading.Thread,
) -> None:
    if process.poll() is None:
        process.kill()
        process.wait()
    os.close(master)
    os.close(output_read_file_descriptor)
    reader.join(timeout=2)


def test_space_pauses_replays_and_q_quits(fakes: Fakes, tmp_path: Path) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text("one two three four five six seven eight nine ten eleven twelve")
    environment, play_log = engine_log_environment(tmp_path, delay="0.3")

    process, master, output_read_file_descriptor, _ = spawn(
        fakes, tmp_path, doc, environment
    )
    buffer = bytearray()
    reader = threading.Thread(
        target=read_loop, args=(output_read_file_descriptor, buffer), daemon=True
    )
    reader.start()

    try:
        assert wait_until(lambda: b"1/1" in buffer), bytes(buffer)
        assert wait_until(
            lambda: play_log.exists() and len(play_log.read_text().splitlines()) == 1
        ), "stream started"

        os.write(master, b" ")
        assert wait_until(lambda: b"paused" in buffer), bytes(buffer)
        time.sleep(0.7)
        lines = play_log.read_text().splitlines()
        assert len(lines) == 1

        os.write(master, b" ")
        assert wait_until(lambda: len(play_log.read_text().splitlines()) == 2), (
            "replayed"
        )
        lines = play_log.read_text().splitlines()
        assert lines[1] == lines[0]
        assert b"resumed" in bytes(buffer)

        os.write(master, b"q")
        assert process.wait(timeout=10) == 0
        assert b"stopped at 1/1" in bytes(buffer)
    finally:
        cleanup(process, master, output_read_file_descriptor, reader)


def test_chained_advance_without_keys(fakes: Fakes, tmp_path: Path) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text("First paragraph words.\n\nSecond paragraph words.")
    environment, play_log = engine_log_environment(tmp_path, delay="0.05")

    process, master, output_read_file_descriptor, _ = spawn(
        fakes, tmp_path, doc, environment
    )
    buffer = bytearray()
    reader = threading.Thread(
        target=read_loop, args=(output_read_file_descriptor, buffer), daemon=True
    )
    reader.start()

    try:
        assert wait_until(lambda: b"1/2" in buffer), bytes(buffer)
        assert wait_until(lambda: b"2/2" in bytes(buffer)), bytes(buffer)
        assert wait_until(
            lambda: play_log.exists() and len(play_log.read_text().splitlines()) == 2
        ), "both paragraphs streamed"
        os.write(master, b"q")
        exit_code = process.wait(timeout=10)
        assert exit_code == 0, exit_code
        assert texts_played(fakes.say_log, play_log) == [
            "First paragraph words.",
            "Second paragraph words.",
        ]
    except OSError:
        pass
    finally:
        cleanup(process, master, output_read_file_descriptor, reader)


def test_playback_error_waits_for_space(fakes: Fakes, tmp_path: Path) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text("only paragraph")

    environment, play_log = engine_log_environment(tmp_path, delay="0.3", fail_at="1")
    process, master, output_read_file_descriptor, error_log_path = spawn(
        fakes, tmp_path, doc, environment
    )
    buffer = bytearray()
    reader = threading.Thread(
        target=read_loop, args=(output_read_file_descriptor, buffer), daemon=True
    )
    reader.start()

    try:
        assert wait_until(lambda: b"device error" in buffer), bytes(buffer)
        err = error_log_path.read_text()
        assert "playback failed" in err
        os.write(master, b" ")
        assert wait_until(
            lambda: play_log.exists() and len(play_log.read_text().splitlines()) == 1
        ), "replayed after error"
        os.write(master, b"q")
        assert process.wait(timeout=10) == 1
    finally:
        cleanup(process, master, output_read_file_descriptor, reader)


def test_corrupt_file_pauses_and_n_skips(fakes: Fakes, tmp_path: Path) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text("first paragraph with CRASHER inside.\n\nsecond paragraph is fine.")

    environment, play_log = engine_log_environment(tmp_path, delay="0.3")
    environment = dict(environment, FAKE_SAY_CORRUPT_TEXT="CRASHER")
    process, master, output_read_file_descriptor, error_log_path = spawn(
        fakes, tmp_path, doc, environment
    )
    buffer = bytearray()
    reader = threading.Thread(
        target=read_loop, args=(output_read_file_descriptor, buffer), daemon=True
    )
    reader.start()

    try:
        assert wait_until(lambda: b"1/2" in buffer), bytes(buffer)
        assert wait_until(lambda: b"device error" in buffer), bytes(buffer)
        err = error_log_path.read_text()
        assert "playback failed" in err
        assert "could not load" in err
        assert texts_played(fakes.say_log, play_log) == []
        os.write(master, b"n")
        assert wait_until(lambda: b"2/2" in buffer), bytes(buffer)
        assert wait_until(lambda: b"done (with errors)" in buffer), bytes(buffer)
        assert process.wait(timeout=10) == 1
        assert texts_played(fakes.say_log, play_log) == ["second paragraph is fine."]
    finally:
        cleanup(process, master, output_read_file_descriptor, reader)
