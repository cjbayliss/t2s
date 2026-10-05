import functools
import queue
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from t2s.engines import (
    AudioLibrary,
    StreamEngine,
    engine_close,
    engine_current_index,
    engine_play,
    engine_prime,
    engine_stop_stream,
    ensure_device,
    load_audio_library,
    load_miniaudio,
    make_engine,
    make_stream_engine,
    pull,
    pull_frames,
    set_stream_format,
)
from t2s.pure import (
    EngineEvent,
    StreamChained,
    StreamCrashed,
    StreamFinished,
    StreamState,
    frames_to_bytes,
    gap_bytes,
    stream_play,
)

try:
    import miniaudio
except ImportError:
    miniaudio = None


def read_raw(engine: StreamEngine, path: Path) -> bytes:
    return path.read_bytes()


def make_raw_engine(
    chunk_bytes: int = 1102, gap_ms: int = 0, fail_at: int = 0
) -> StreamEngine:
    return make_stream_engine(
        load=read_raw, chunk_bytes=chunk_bytes, gap_ms=gap_ms, fail_at=fail_at
    )


@functools.cache
def audio_available() -> bool:
    if miniaudio is None:
        return False
    try:
        device = miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=22050,
            buffersize_msec=60,
        )
    except Exception:
        return False
    device.close()
    return True


def test_stream_transitions_are_pure() -> None:
    initial = StreamState()
    played = stream_play(initial, 0, Path("a.raw"), b"\x01\x02")
    assert initial == StreamState()
    assert played.current_index == 0
    assert played.data == b"\x01\x02" and played.data_pos == 0


def make_wav(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def drain(engine: StreamEngine, count: int) -> list[bytes]:
    gen = pull(engine)
    return [next(gen) for _ in range(count)]


def pop_event(engine: StreamEngine) -> EngineEvent | None:
    try:
        return engine.events.get_nowait()
    except queue.Empty:
        return None


def events(engine: StreamEngine) -> list[EngineEvent]:
    collected: list[EngineEvent] = []
    while (event := pop_event(engine)) is not None:
        collected.append(event)
    return collected


def test_zero_gap_chain(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = make_raw_engine(chunk_bytes=2)
    engine_play(engine, 0, first)
    engine_prime(engine, 1, second)

    chunks = drain(engine, 5)

    assert b"".join(chunks) == (b"\x01\x02" * 3) + b"\x03\x04" * 2
    assert events(engine) == [StreamFinished(0, True), StreamChained(1)]
    assert engine_current_index(engine) == 1


def test_unchained_end_goes_silent(tmp_path: Path) -> None:
    only = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = make_raw_engine(chunk_bytes=2)
    engine_play(engine, 0, only)

    chunks = drain(engine, 4)

    assert chunks[:2] == [b"\x01\x02", b"\x01\x02"]
    assert chunks[2:] == [b"\x00\x00", b"\x00\x00"]
    assert events(engine) == [StreamFinished(0, False)]
    assert engine_current_index(engine) is None


def test_stop_stream_and_replay(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 4)
    engine = make_raw_engine(chunk_bytes=2)
    engine_play(engine, 0, data)

    first = drain(engine, 1)
    engine_stop_stream(engine)
    assert engine_current_index(engine) is None

    engine_play(engine, 0, data)
    again = drain(engine, 4)
    assert b"".join(again) == b"\x01\x02" * 4
    assert first == [b"\x01\x02"]


def test_fail_start_simulates_device_error(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = make_raw_engine(chunk_bytes=2, fail_at=1)
    engine_play(engine, 0, data)
    chunks = drain(engine, 2)
    assert chunks == [b"\x00\x00", b"\x00\x00"]
    assert engine.events.get_nowait() == StreamCrashed(0, "simulated device error")
    assert engine_current_index(engine) is None


def test_fail_at_counts_starts_not_paragraphs(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = make_raw_engine(chunk_bytes=2, fail_at=1)
    engine_play(engine, 0, data)
    drain(engine, 1)
    engine.events.get_nowait()

    engine_play(engine, 0, data)
    chunks = drain(engine, 2)
    assert b"".join(chunks) == b"\x01\x02" * 2
    assert events(engine) == []


def test_prime_without_chain_is_ignored(tmp_path: Path) -> None:
    a = make_wav(tmp_path, "a.raw", b"\x01\x02")
    c = make_wav(tmp_path, "c.raw", b"\x05\x06")
    engine = make_raw_engine(chunk_bytes=2)
    engine_play(engine, 0, a)
    engine_prime(engine, 2, c)
    drain(engine, 3)
    engine_events = events(engine)
    assert not any(isinstance(e, StreamChained) for e in engine_events)
    assert any(isinstance(e, StreamFinished) for e in engine_events)


def test_set_stream_format_updates_gap(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = make_raw_engine(chunk_bytes=2, gap_ms=2)
    set_stream_format(engine, 48000, 1)
    gap = round(2 * 48000 / 1000) * 2
    assert gap == 192
    engine_play(engine, 0, first)
    engine_prime(engine, 1, second)

    chunks = pull(engine)
    joined = b"".join(next(chunks) for _ in range(120))

    body = (b"\x01\x02" * 2) + bytes(gap) + (b"\x03\x04" * 2)
    assert joined.startswith(body)
    assert joined[len(body) :].strip(b"\x00") == b""


def test_frames_to_bytes_and_gap_bytes() -> None:
    assert frames_to_bytes(10, 1) == 20
    assert frames_to_bytes(10, 2) == 40
    assert gap_bytes(0, 22050, 1) == 0
    assert gap_bytes(2, 22050, 1) == 88
    assert gap_bytes(2, 48000, 1) == 192
    assert gap_bytes(1, 44100, 2) == 176


def test_gap_bytes_matches_engine_math() -> None:
    assert gap_bytes(2, 22050, 1) == round(2 * 22050 / 1000) * 2


def test_detect_output_rate() -> None:
    from t2s.engines import detect_output_rate

    rate = detect_output_rate()
    assert isinstance(rate, int) and 8000 <= rate <= 384000
    assert detect_output_rate() == rate


def test_default_data_format_matches_device() -> None:
    from t2s.engines import default_data_format, detect_output_rate

    assert default_data_format() == f"LEI16@{detect_output_rate()}"


def test_prime_missing_file_is_best_effort(tmp_path: Path) -> None:
    engine = make_raw_engine(chunk_bytes=2)
    engine_prime(engine, 0, tmp_path / "does-not-exist.raw")
    assert engine_current_index(engine) is None


def failing_load(engine: StreamEngine, path: Path) -> bytes:
    raise RuntimeError("decode exploded")


def test_play_load_failure_becomes_crash_event(tmp_path: Path) -> None:
    engine = make_stream_engine(load=failing_load, chunk_bytes=2)
    engine_play(engine, 3, tmp_path / "x.raw")
    assert engine_current_index(engine) is None
    assert events(engine) == [
        StreamCrashed(3, "could not load x.raw: RuntimeError: decode exploded")
    ]


def test_prime_load_failure_is_silent(tmp_path: Path) -> None:
    engine = make_stream_engine(load=failing_load, chunk_bytes=2)
    engine_prime(engine, 0, tmp_path / "x.raw")
    assert engine_current_index(engine) is None
    assert events(engine) == []


def missing_file_detail(name: str, path: Path) -> str:
    return (
        f"could not load {name}: FileNotFoundError: "
        f"[Errno 2] No such file or directory: '{path}'"
    )


def test_play_missing_file_reports_crash(tmp_path: Path) -> None:
    engine = make_raw_engine(chunk_bytes=2)
    missing = tmp_path / "missing.raw"
    engine_play(engine, 1, missing)
    detail = missing_file_detail("missing.raw", missing)
    assert engine_current_index(engine) is None
    assert events(engine) == [StreamCrashed(1, detail)]


def test_failed_play_leaves_current_stream_alone(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = make_raw_engine(chunk_bytes=2)
    engine_play(engine, 0, data)
    drain(engine, 1)
    missing = tmp_path / "missing.raw"
    engine_play(engine, 1, missing)
    detail = missing_file_detail("missing.raw", missing)
    chunks = drain(engine, 2)
    assert chunks == [b"\x01\x02", b"\x00\x00"]
    assert events(engine) == [
        StreamCrashed(1, detail),
        StreamFinished(0, False),
    ]


class FakeMiniaudioError(Exception):
    pass


def fake_decode_module() -> Any:
    class Decoder:
        MiniaudioError = FakeMiniaudioError

        @staticmethod
        def wav_read_file_s16(path: str) -> object:
            raise FakeMiniaudioError("junk data")

    return cast(Any, Decoder)


def test_miniaudio_decode_error_is_translated(tmp_path: Path) -> None:
    audio = AudioLibrary(module=fake_decode_module())
    with pytest.raises(RuntimeError, match="junk data"):
        load_miniaudio(audio, tmp_path / "x.wav")


def fake_nodevice_module() -> Any:
    def playback_device(**kwargs: object) -> object:
        raise FakeMiniaudioError("no output device")

    return cast(
        Any,
        SimpleNamespace(
            MiniaudioError=FakeMiniaudioError,
            SampleFormat=SimpleNamespace(SIGNED16="s16"),
            PlaybackDevice=playback_device,
        ),
    )


def test_device_open_failure_is_translated(tmp_path: Path) -> None:
    path = tmp_path / "a.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x00" * 4)
    engine = make_stream_engine(audio=AudioLibrary(module=fake_nodevice_module()))
    with pytest.raises(RuntimeError, match="no output device"):
        ensure_device(engine, path)


def test_gap_warning_fires_only_for_afplay(capsys: pytest.CaptureFixture[str]) -> None:
    make_engine("afplay", ("afplay",), gap_ms=150)
    assert "--gap" in capsys.readouterr().err
    make_engine("afplay", ("afplay",), gap_ms=0)
    assert capsys.readouterr().err == ""
    make_engine("test", (), gap_ms=150)
    assert capsys.readouterr().err == ""
    make_engine("auto", ("afplay",), gap_ms=150, audio=load_audio_library())
    assert capsys.readouterr().err == ""
    make_engine("auto", ("afplay",), gap_ms=150, audio=None)
    assert "--gap" in capsys.readouterr().err


def test_gap_inserts_silence_between_paragraphs(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = make_raw_engine(chunk_bytes=2, gap_ms=2)
    expected_gap = round(2 * 22050 / 1000) * 2
    assert expected_gap == 88
    engine_play(engine, 0, first)
    engine_prime(engine, 1, second)

    chunks = pull(engine)
    joined = b"".join(next(chunks) for _ in range(60))

    body = (b"\x01\x02" * 3) + bytes(expected_gap) + (b"\x03\x04" * 2)
    assert joined.startswith(body)
    assert joined[len(body) :].strip(b"\x00") == b""
    assert events(engine) == [
        StreamFinished(0, True),
        StreamChained(1),
        StreamFinished(1, False),
    ]


def test_zero_gap_is_the_default(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = make_raw_engine(chunk_bytes=2)
    engine_play(engine, 0, first)
    engine_prime(engine, 1, second)

    chunks = drain(engine, 5)
    assert b"".join(chunks) == (b"\x01\x02" * 3) + (b"\x03\x04" * 2)


def test_explicit_play_has_no_leading_gap(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 4)
    engine = make_raw_engine(chunk_bytes=2, gap_ms=500)
    engine_play(engine, 0, data)
    chunks = drain(engine, 2)
    assert chunks == [b"\x01\x02", b"\x01\x02"]


def test_gap_across_request_boundaries(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = make_raw_engine(chunk_bytes=2, gap_ms=10)
    expected_gap = round(10 * 22050 / 1000) * 2
    engine_play(engine, 0, first)
    engine_prime(engine, 1, second)

    frames = pull_frames(engine)
    next(frames)
    got = frames.send(230)
    expected = (b"\x01\x02" * 2) + bytes(expected_gap) + (b"\x03\x04" * 2)
    assert got.startswith(expected)
    assert got[len(expected) :].strip(b"\x00") == b""


def test_pull_frames_protocol(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = make_raw_engine(chunk_bytes=2)
    engine_play(engine, 0, first)
    engine_prime(engine, 1, second)

    frames = pull_frames(engine)
    assert next(frames) == b""
    assert frames.send(2) == b"\x01\x02\x01\x02"
    assert frames.send(3) == b"\x01\x02\x03\x04\x03\x04"
    assert frames.send(2) == b"\x00\x00\x00\x00"
    assert events(engine) == [
        StreamFinished(0, True),
        StreamChained(1),
        StreamFinished(1, False),
    ]


def test_pull_frames_single_request_spans_both(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = make_raw_engine(chunk_bytes=2)
    engine_play(engine, 0, first)
    engine_prime(engine, 1, second)

    frames = pull_frames(engine)
    next(frames)
    assert frames.send(5) == (b"\x01\x02" * 3) + (b"\x03\x04" * 2)
    assert frames.send(2) == bytes(4)
    assert events(engine) == [
        StreamFinished(0, True),
        StreamChained(1),
        StreamFinished(1, False),
    ]


def test_miniaudio_real_device(tmp_path: Path) -> None:
    if not audio_available():
        pytest.skip("no usable audio output device")
    import time

    path = tmp_path / "beep.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x00" * 4410)

    engine = make_stream_engine(audio=load_audio_library())
    engine_play(engine, 0, path)
    deadline = time.monotonic() + 5
    while engine_current_index(engine) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    engine_close(engine)
    assert engine_current_index(engine) is None


def test_device_format_follows_file_header(tmp_path: Path) -> None:
    if not audio_available():
        pytest.skip("no usable audio output device")

    path = tmp_path / "hi.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(b"\x00\x00" * 4800)

    engine = make_stream_engine(audio=load_audio_library())
    engine_play(engine, 0, path)
    device = engine.cell.device
    assert device is not None
    rate = device.sample_rate
    engine_close(engine)
    assert rate == 48000


def test_miniaudio_plays_in_real_time(tmp_path: Path) -> None:
    if not audio_available():
        pytest.skip("no usable audio output device")
    import time

    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\x00\x00" * 22050 * 2)

    engine = make_stream_engine(audio=load_audio_library())
    engine_play(engine, 0, path)
    start = time.monotonic()
    deadline = start + 30
    while engine_current_index(engine) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    elapsed = time.monotonic() - start
    engine_close(engine)
    assert engine_current_index(engine) is None
    assert 1.5 <= elapsed <= 6.0, f"2 s of audio drained in {elapsed:.1f}s"
