import importlib.util
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import final

import pytest

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("COVERAGE_PROCESS_START", str(REPO / "pyproject.toml"))
FAKE_SAY_SRC = Path(__file__).resolve().parent / "fake_say.py"
FAKE_PLAY_SRC = Path(__file__).resolve().parent / "fake_play.py"
FAKE_PTTS_SRC = Path(__file__).resolve().parent / "fake_ptts.py"


def wait_until(
    predicate: Callable[[], bool], timeout: float = 10.0, step: float = 0.02
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


@final
@dataclass(frozen=True, slots=True)
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


@final
@dataclass(frozen=True, slots=True)
class FakePhonon:
    module_directory: Path
    log: Path

    def lines(self) -> tuple[str, ...]:
        if not self.log.exists():
            return ()
        return tuple(self.log.read_text(encoding="utf-8").splitlines())

    def played_texts(self, play_log: Path) -> list[str]:
        mapping: dict[str, str] = {}
        for line in self.lines():
            kind, _, rest = line.partition("\t")
            if kind == "save":
                path, _, text = rest.partition("\t")
                mapping[path.removesuffix(".part")] = text
        if not play_log.exists():
            return []
        return [
            mapping.get(path, path)
            for path in play_log.read_text(encoding="utf-8").splitlines()
            if path.strip()
        ]

    def saved_lines(self) -> tuple[str, ...]:
        return tuple(line for line in self.lines() if line.startswith("save\t"))

    def load_lines(self) -> tuple[str, ...]:
        return tuple(line for line in self.lines() if line.startswith("load\t"))


@pytest.fixture
def fake_phonon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakePhonon:
    module_directory = tmp_path / "ptts-module"
    module_directory.mkdir()
    module_path = module_directory / "ptts.py"
    module_path.write_text(FAKE_PTTS_SRC.read_text(encoding="utf-8"), encoding="utf-8")
    log = tmp_path / "phonon.log"
    monkeypatch.setenv("T2S_FAKE_PTTS_LOG", str(log))
    return FakePhonon(module_directory=module_directory, log=log)


@pytest.fixture
def fake_ptts(fake_phonon: FakePhonon, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("ptts", FAKE_PTTS_SRC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "ptts", module)
    return module


def run_t2s(
    arguments: list[str],
    fakes: Fakes | None = None,
    environment_extra: dict[str, str] | None = None,
    input: bytes | None = None,
    timeout: float = 30.0,
    backend_bin: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "t2s"]
    if fakes is not None:
        command += ["--play-bin", fakes.play_binary]
        if backend_bin:
            command += ["--backend-bin", fakes.say_binary]
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
