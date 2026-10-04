import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE_SAY_SRC = Path(__file__).resolve().parent / "fake_say.py"
FAKE_PLAY_SRC = Path(__file__).resolve().parent / "fake_play.py"


@dataclass(frozen=True)
class Fakes:
    say_bin: str
    play_bin: str
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
    return [mapping.get(p, p) for p in play_log.read_text().splitlines() if p.strip()]


def engine_log_env(
    tmp_path: Path, delay: str = "0.05", fail_at: str | None = None
) -> tuple[dict[str, str], Path]:
    log = tmp_path / "engine-play.log"
    env = {"T2S_TEST_PLAY_LOG": str(log), "T2S_TEST_PLAY_DELAY": delay}
    if fail_at:
        env["T2S_TEST_PLAY_FAIL_AT"] = fail_at
    return env, log


def _wrapper(tmp_path: Path, name: str, src: Path, env: dict[str, Path]) -> str:
    script = tmp_path / name
    lines = ["#!/bin/sh"]
    for key, value in env.items():
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
    return Fakes(say_bin=say, play_bin=play, play_log=play_log, say_log=say_log)


def run_t2s(
    args: list[str],
    fakes: Fakes | None = None,
    env_extra: dict[str, str] | None = None,
    input: bytes | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "t2s"]
    if fakes is not None:
        cmd += ["--say-bin", fakes.say_bin, "--play-bin", fakes.play_bin]
    cmd += args
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        cmd, input=input, capture_output=True, env=env, timeout=timeout, cwd=REPO
    )
    return subprocess.CompletedProcess(
        r.args,
        r.returncode,
        r.stdout.decode("utf-8", "replace"),
        r.stderr.decode("utf-8", "replace"),
    )
