import functools
import queue
import wave
from pathlib import Path

import pytest

from t2s.engines import HAVE_MINIAUDIO, ChainEngine, MiniaudioEngine
from t2s.pure import (
    SAMPLE_RATE,
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


class RawEngine(ChainEngine):
    def _load(self, path: Path) -> bytes:
        return path.read_bytes()


@functools.cache
def audio_available() -> bool:
    if not HAVE_MINIAUDIO:
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
    s = StreamState()
    t = stream_play(s, 0, Path("a.raw"), b"\x01\x02")
    assert s == StreamState()
    assert t.cur == 0 and t.data == b"\x01\x02" and t.pos == 0


def make_wav(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


def drain(engine: RawEngine, n: int) -> list[bytes]:
    gen = engine._pull()
    return [next(gen) for _ in range(n)]


def take(engine: RawEngine) -> EngineEvent | None:
    try:
        return engine.events.get_nowait()
    except queue.Empty:
        return None


def events(engine: RawEngine) -> list[EngineEvent]:
    out: list[EngineEvent] = []
    while (ev := take(engine)) is not None:
        out.append(ev)
    return out


def test_zero_gap_chain(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, first)
    engine.prime(1, second)

    chunks = drain(engine, 5)

    assert b"".join(chunks) == (b"\x01\x02" * 3) + b"\x03\x04" * 2
    assert events(engine) == [StreamFinished(0, True), StreamChained(1)]
    assert engine.current_index() == 1


def test_unchained_end_goes_silent(tmp_path: Path) -> None:
    only = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, only)

    chunks = drain(engine, 4)

    assert chunks[:2] == [b"\x01\x02", b"\x01\x02"]
    assert chunks[2:] == [b"\x00\x00", b"\x00\x00"]
    assert events(engine) == [StreamFinished(0, False)]
    assert engine.current_index() is None


def test_stop_stream_and_replay(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 4)
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, data)

    first = drain(engine, 1)
    engine.stop_stream()
    assert engine.current_index() is None

    engine.play(0, data)
    again = drain(engine, 4)
    assert b"".join(again) == b"\x01\x02" * 4
    assert first == [b"\x01\x02"]


def test_fail_start_simulates_device_error(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = RawEngine(chunk_bytes=2, fail_at=1)
    engine.play(0, data)
    chunks = drain(engine, 2)
    assert chunks == [b"\x00\x00", b"\x00\x00"]
    assert engine.events.get_nowait() == StreamCrashed(0, "simulated device error")
    assert engine.current_index() is None


def test_fail_at_counts_starts_not_paragraphs(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = RawEngine(chunk_bytes=2, fail_at=1)
    engine.play(0, data)
    drain(engine, 1)
    engine.events.get_nowait()

    engine.play(0, data)
    chunks = drain(engine, 2)
    assert b"".join(chunks) == b"\x01\x02" * 2
    assert events(engine) == []


def test_prime_without_chain_is_ignored(tmp_path: Path) -> None:
    a = make_wav(tmp_path, "a.raw", b"\x01\x02")
    c = make_wav(tmp_path, "c.raw", b"\x05\x06")
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, a)
    engine.prime(2, c)
    drain(engine, 3)
    evs = events(engine)
    assert not any(isinstance(e, StreamChained) for e in evs)
    assert any(isinstance(e, StreamFinished) for e in evs)


def test_set_stream_format_updates_gap(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2, gap_ms=2)
    engine.set_stream_format(48000, 1)
    gap = round(2 * 48000 / 1000) * 2
    assert gap == 192
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull()
    joined = b"".join(next(gen) for _ in range(120))

    body = (b"\x01\x02" * 2) + bytes(gap) + (b"\x03\x04" * 2)
    assert joined.startswith(body)
    assert joined[len(body) :].strip(b"\x00") == b""


def test_frames_to_bytes_and_gap_bytes() -> None:
    assert frames_to_bytes(10, 1) == 20
    assert frames_to_bytes(10, 2) == 40
    assert gap_bytes(0, 22050, 1) == 0
    assert gap_bytes(2, SAMPLE_RATE, 1) == 88
    assert gap_bytes(2, 48000, 1) == 192
    assert gap_bytes(1, 44100, 2) == 176


def test_gap_bytes_matches_engine_math() -> None:
    assert gap_bytes(2, SAMPLE_RATE, 1) == round(2 * SAMPLE_RATE / 1000) * 2


def test_detect_output_rate() -> None:
    from t2s.engines import detect_output_rate

    rate = detect_output_rate()
    assert isinstance(rate, int) and 8000 <= rate <= 384000
    assert detect_output_rate() == rate


def test_default_data_format_matches_device() -> None:
    from t2s.engines import default_data_format, detect_output_rate

    assert default_data_format() == f"LEI16@{detect_output_rate()}"


def test_prime_missing_file_is_best_effort(tmp_path: Path) -> None:
    engine = RawEngine(chunk_bytes=2)
    engine.prime(0, tmp_path / "does-not-exist.raw")
    assert engine.current_index() is None


def test_gap_inserts_silence_between_paragraphs(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2, gap_ms=2)
    gap_bytes = round(2 * SAMPLE_RATE / 1000) * 2
    assert gap_bytes == 88
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull()
    chunks = [next(gen) for _ in range(60)]
    joined = b"".join(chunks)

    body = (b"\x01\x02" * 3) + bytes(gap_bytes) + (b"\x03\x04" * 2)
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
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, first)
    engine.prime(1, second)

    chunks = drain(engine, 5)
    assert b"".join(chunks) == (b"\x01\x02" * 3) + (b"\x03\x04" * 2)


def test_explicit_play_has_no_leading_gap(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 4)
    engine = RawEngine(chunk_bytes=2, gap_ms=500)
    engine.play(0, data)
    chunks = drain(engine, 2)
    assert chunks == [b"\x01\x02", b"\x01\x02"]


def test_gap_across_request_boundaries(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2, gap_ms=10)
    gap_bytes = round(10 * SAMPLE_RATE / 1000) * 2
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull_frames()
    next(gen)
    got = gen.send(230)
    expected = (b"\x01\x02" * 2) + bytes(gap_bytes) + (b"\x03\x04" * 2)
    assert got.startswith(expected)
    assert got[len(expected) :].strip(b"\x00") == b""


def test_pull_frames_protocol(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull_frames()
    assert next(gen) == b""
    assert gen.send(2) == b"\x01\x02\x01\x02"
    assert gen.send(3) == b"\x01\x02\x03\x04\x03\x04"
    assert gen.send(2) == b"\x00\x00\x00\x00"
    assert events(engine) == [
        StreamFinished(0, True),
        StreamChained(1),
        StreamFinished(1, False),
    ]


def test_pull_frames_single_request_spans_both(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull_frames()
    next(gen)
    assert gen.send(5) == (b"\x01\x02" * 3) + (b"\x03\x04" * 2)
    assert gen.send(2) == bytes(4)
    assert events(engine) == [
        StreamFinished(0, True),
        StreamChained(1),
        StreamFinished(1, False),
    ]


def test_miniaudio_real_device(tmp_path: Path) -> None:
    if not audio_available():
        pytest.skip("no usable audio output device")
    import time as time_mod

    path = tmp_path / "beep.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 4410)

    engine = MiniaudioEngine()
    engine.play(0, path)
    deadline = time_mod.monotonic() + 5
    while engine.current_index() is not None and time_mod.monotonic() < deadline:
        time_mod.sleep(0.02)
    engine.close()
    assert engine.current_index() is None


def test_device_format_follows_file_header(tmp_path: Path) -> None:
    if not audio_available():
        pytest.skip("no usable audio output device")

    path = tmp_path / "hi.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 4800)

    engine = MiniaudioEngine()
    engine.play(0, path)
    rate = engine._device.sample_rate
    engine.close()
    assert rate == 48000


def test_miniaudio_plays_in_real_time(tmp_path: Path) -> None:
    if not audio_available():
        pytest.skip("no usable audio output device")
    import time as time_mod

    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 22050 * 2)

    engine = MiniaudioEngine()
    engine.play(0, path)
    start = time_mod.monotonic()
    deadline = start + 30
    while engine.current_index() is not None and time_mod.monotonic() < deadline:
        time_mod.sleep(0.02)
    elapsed = time_mod.monotonic() - start
    engine.close()
    assert engine.current_index() is None
    assert 1.5 <= elapsed <= 6.0, f"2 s of audio drained in {elapsed:.1f}s"
