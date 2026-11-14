import ast
import math
import sys
import wave
from array import array
from pathlib import Path

import pytest

from t2s.engines import (
    load_playback_audio,
    make_process_engine,
    make_stream_engine,
    process_engine_play,
)
from t2s.monads import io_run
from t2s.pure import StreamFinished
from t2s.stretch import time_stretched


def sine_bytes(rate: int, seconds: float, frequency: int = 220) -> bytes:
    count = int(rate * seconds)
    return array(
        "h",
        (
            int(12000 * math.sin(2 * math.pi * frequency * index / rate))
            for index in range(count)
        ),
    ).tobytes()


def interleaved_samples(rate: int, seconds: float, channels: int) -> array[int]:
    mono = array("h", sine_bytes(rate, seconds))
    return array("h", (sample for sample in mono for _ in range(channels)))


def write_wav(path: Path, rate: int, payload: bytes) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(payload)
    return path


def test_time_stretched_passes_data_through_unchanged() -> None:
    data = sine_bytes(22050, 1.0)
    tiny = array("h", (0, 0, 0)).tobytes()
    assert time_stretched(data, 22050, 1, 1.0) == data
    assert time_stretched(b"", 22050, 1, 2.0) == b""
    assert time_stretched(tiny, 22050, 1, 2.0) == tiny


@pytest.mark.parametrize("channels", (1, 2))
def test_output_length_scales_inversely_with_speed(channels: int) -> None:
    rate = 22050
    data = interleaved_samples(rate, 2.0, channels)
    for speed in (0.5, 1.5, 2.0, 3.0):
        output = array("h", time_stretched(data.tobytes(), rate, channels, speed))
        assert len(output) % channels == 0
        expected = len(data) / speed
        assert abs(len(output) - expected) / expected < 0.05


def test_load_playback_audio_applies_engine_speed(tmp_path: Path) -> None:
    rate = 22050
    payload = sine_bytes(rate, 1.0)
    path = write_wav(tmp_path / "speech.wav", rate, payload)
    expected = ((1.0, payload), (2.0, time_stretched(payload, rate, 1, 2.0)))
    for speed, stretched in expected:
        engine = io_run(make_stream_engine(speed=speed))
        assert io_run(load_playback_audio(engine, path)) == stretched


@pytest.mark.parametrize(
    ("speed", "rate_arguments"),
    ((1.5, ["-r", "1.5"]), (1.0, [])),
)
def test_process_engine_forwards_rate_only_when_speed_differs(
    tmp_path: Path, speed: float, rate_arguments: list[str]
) -> None:
    argv_path = tmp_path / "argv.txt"
    script = (
        "import sys, pathlib; "
        f"pathlib.Path({str(argv_path)!r}).write_text(repr(sys.argv[1:]))"
    )
    engine = io_run(make_process_engine((sys.executable, "-c", script), speed=speed))
    io_run(process_engine_play(engine, 0, tmp_path / "track.wav"))
    assert engine.events.get(timeout=10) == StreamFinished(0, False)
    arguments = ast.literal_eval(argv_path.read_text(encoding="utf-8"))
    assert arguments == [*rate_arguments, str(tmp_path / "track.wav")]
