import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("COVERAGE_PROCESS_START", str(REPO / "pyproject.toml"))
FAKE_SAY_SRC = Path(__file__).resolve().parent / "fake_say.py"
FAKE_PLAY_SRC = Path(__file__).resolve().parent / "fake_play.py"


@dataclass(frozen=True)
class Fakes:
    say_binary: str
    play_binary: str
    play_log: Path
    say_log: Path

    def played_texts(self) -> list[str]:
        return texts_played(self.say_log, self.play_log)


def texts_played(say_log: Path, play_log: Path) -> list[str]:
    mapping: dict[str, str] = {}
    if say_log.exists():
        for line in say_log.read_text().splitlines():
            path, _, text = line.partition("\t")
            mapping[path] = text
    if not play_log.exists():
        return []
    return [
        mapping.get(path, path)
        for path in play_log.read_text().splitlines()
        if path.strip()
    ]


def engine_log_environment(
    tmp_path: Path, delay: str = "0.05", fail_at: str | None = None
) -> tuple[dict[str, str], Path]:
    log = tmp_path / "engine-play.log"
    environment = {"T2S_TEST_PLAY_LOG": str(log), "T2S_TEST_PLAY_DELAY": delay}
    if fail_at:
        environment["T2S_TEST_PLAY_FAIL_AT"] = fail_at
    return environment, log


def _wrapper(tmp_path: Path, name: str, src: Path, environment: dict[str, Path]) -> str:
    script = tmp_path / name
    lines = ["#!/bin/sh"]
    for key, value in environment.items():
        lines += [f"{key}='{value}'", f"export {key}"]
    lines += [f"exec '{sys.executable}' '{src}' \"$@\""]
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def fakes(tmp_path: Path) -> Fakes:
    if os.name != "posix":
        pytest.skip("fake binaries require a POSIX shell")
    play_log = tmp_path / "play.log"
    say_log = tmp_path / "say.log"
    say = _wrapper(tmp_path, "fake_say", FAKE_SAY_SRC, {"FAKE_SAY_LOG": say_log})
    play = _wrapper(tmp_path, "fake_play", FAKE_PLAY_SRC, {"FAKE_PLAY_LOG": play_log})
    return Fakes(say_binary=say, play_binary=play, play_log=play_log, say_log=say_log)


def run_t2s(
    arguments: list[str],
    fakes: Fakes | None = None,
    environment_extra: dict[str, str] | None = None,
    input: bytes | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "t2s"]
    if fakes is not None:
        command += ["--say-bin", fakes.say_binary, "--play-bin", fakes.play_binary]
    command += arguments
    environment = os.environ.copy()
    if environment_extra:
        environment.update(environment_extra)
    result = subprocess.run(
        command,
        input=input,
        stdin=subprocess.DEVNULL if input is None else None,
        capture_output=True,
        env=environment,
        timeout=timeout,
        cwd=REPO,
        start_new_session=True,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )
