"""Unit tests for the zero-gap chaining engine (no audio device needed).

Uses a trivial subclass of ChainEngine whose "files" are plain byte files,
and consumes the pull generator directly so chunk boundaries and events
can be asserted exactly.
"""

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
    stream_play,
)


class RawEngine(ChainEngine):
    """Treats each file's bytes as raw s16 samples."""

    def _load(self, path: Path) -> bytes:
        return path.read_bytes()


def test_stream_transitions_are_pure() -> None:
    """stream_play derives a new state; the state it was given is untouched."""
    s = StreamState()
    t = stream_play(s, 0, Path("a.raw"), b"\x01\x02")
    assert s == StreamState()  # input value unchanged
    assert t.cur == 0 and t.data == b"\x01\x02" and t.pos == 0


def make_wav(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


def drain(engine: RawEngine, n: int) -> list[bytes]:
    """Pull n chunks from the engine's stream."""
    gen = engine._pull()
    return [next(gen) for _ in range(n)]


def events(engine: RawEngine) -> list[EngineEvent]:
    out: list[EngineEvent] = []
    while True:
        # PERF203: the queue API is exception-based.
        try:
            out.append(engine.events.get_nowait())
        except queue.Empty:  # noqa: PERF203
            return out


def test_zero_gap_chain(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, first)
    engine.prime(1, second)

    chunks = drain(engine, 5)  # ¶1 (3 chunks) + all of ¶2 (2 chunks)

    # No silence anywhere: the stream is exactly ¶1 + ¶2, seamlessly.
    assert b"".join(chunks) == (b"\x01\x02" * 3) + b"\x03\x04" * 2
    assert events(engine) == [StreamFinished(0, True), StreamChained(1)]
    assert engine.current_index() == 1


def test_unchained_end_goes_silent(tmp_path: Path) -> None:
    only = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, only)

    chunks = drain(engine, 4)

    assert chunks[:2] == [b"\x01\x02", b"\x01\x02"]
    assert chunks[2:] == [b"\x00\x00", b"\x00\x00"]  # idle silence
    assert events(engine) == [StreamFinished(0, False)]
    assert engine.current_index() is None


def test_stop_stream_and_replay(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 4)
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, data)

    first = drain(engine, 1)
    engine.stop_stream()
    assert engine.current_index() is None

    engine.play(0, data)  # replay from the beginning
    again = drain(engine, 4)
    assert b"".join(again) == b"\x01\x02" * 4
    assert first == [b"\x01\x02"]


def test_fail_start_simulates_device_error(tmp_path: Path) -> None:
    """fail_at is a pure part of the stream state: the 1-based stream
    start to crash (here: the first) never emits a Started event."""
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = RawEngine(chunk_bytes=2, fail_at=1)
    engine.play(0, data)
    chunks = drain(engine, 2)
    assert chunks == [b"\x00\x00", b"\x00\x00"]  # never started
    assert engine.events.get_nowait() == StreamCrashed(0, "simulated device error")
    assert engine.current_index() is None


def test_fail_at_counts_starts_not_paragraphs(tmp_path: Path) -> None:
    """The crash decision follows stream starts: after the first start
    fails, replaying the same paragraph (start #2) plays normally."""
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    engine = RawEngine(chunk_bytes=2, fail_at=1)
    engine.play(0, data)
    drain(engine, 1)  # crashed: the failed start still counted
    engine.events.get_nowait()

    engine.play(0, data)  # replay: second start, past fail_at
    chunks = drain(engine, 2)
    assert b"".join(chunks) == b"\x01\x02" * 2
    assert events(engine) == []  # streamed clean, no crash


def test_prime_without_chain_is_ignored(tmp_path: Path) -> None:
    a = make_wav(tmp_path, "a.raw", b"\x01\x02")
    c = make_wav(tmp_path, "c.raw", b"\x05\x06")
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, a)
    engine.prime(2, c)  # not current+1: must not chain
    drain(engine, 3)
    evs = events(engine)
    assert not any(isinstance(e, StreamChained) for e in evs)
    assert any(isinstance(e, StreamFinished) for e in evs)


def test_set_stream_format_updates_gap(tmp_path: Path) -> None:
    """The gap must be recomputed when the engine learns the real format."""
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2, gap_ms=2)
    engine.set_stream_format(48000, 1)  # e.g. 48 kHz cached files
    gap = round(2 * 48000 / 1000) * 2  # 192 bytes
    assert gap == 192
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull()
    joined = b"".join(next(gen) for _ in range(120))  # 240 B > 4+192+4 body

    body = (b"\x01\x02" * 2) + bytes(gap) + (b"\x03\x04" * 2)
    assert joined.startswith(body)
    assert joined[len(body) :].strip(b"\x00") == b""


def test_detect_output_rate() -> None:
    from t2s.engines import detect_output_rate

    rate = detect_output_rate()
    assert isinstance(rate, int) and 8000 <= rate <= 384000
    assert detect_output_rate() == rate  # stable within a process


def test_default_data_format_matches_device() -> None:
    from t2s.engines import default_data_format, detect_output_rate

    assert default_data_format() == f"LEI16@{detect_output_rate()}"


def test_prime_missing_file_is_best_effort(tmp_path: Path) -> None:
    engine = RawEngine(chunk_bytes=2)
    engine.prime(0, tmp_path / "does-not-exist.raw")  # must not raise
    assert engine.current_index() is None


def test_gap_inserts_silence_between_paragraphs(tmp_path: Path) -> None:
    """--gap: exact silence between chained paragraphs, none elsewhere."""
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)  # 3 frames
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)  # 2 frames
    engine = RawEngine(chunk_bytes=2, gap_ms=2)  # -> round(44.1)=44 frames
    gap_bytes = round(2 * SAMPLE_RATE / 1000) * 2
    assert gap_bytes == 88
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull()
    chunks = [next(gen) for _ in range(60)]
    joined = b"".join(chunks)

    body = (b"\x01\x02" * 3) + bytes(gap_bytes) + (b"\x03\x04" * 2)
    assert joined.startswith(body)
    # everything after ¶2 is idle silence too
    assert joined[len(body) :].strip(b"\x00") == b""
    assert events(engine) == [
        StreamFinished(0, True),
        StreamChained(1),
        StreamFinished(1, False),
    ]


def test_zero_gap_is_the_default(tmp_path: Path) -> None:
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2)  # gap_ms defaults to 0
    engine.play(0, first)
    engine.prime(1, second)

    chunks = drain(engine, 5)
    assert b"".join(chunks) == (b"\x01\x02" * 3) + (b"\x03\x04" * 2)


def test_explicit_play_has_no_leading_gap(tmp_path: Path) -> None:
    data = make_wav(tmp_path, "a.raw", b"\x01\x02" * 4)
    engine = RawEngine(chunk_bytes=2, gap_ms=500)
    engine.play(0, data)
    chunks = drain(engine, 2)
    assert chunks == [b"\x01\x02", b"\x01\x02"]  # starts immediately


def test_gap_across_request_boundaries(tmp_path: Path) -> None:
    """A gap larger than one device request must still be exact."""
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 2)
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)
    engine = RawEngine(chunk_bytes=2, gap_ms=10)  # ~220 frames = 441 bytes
    gap_bytes = round(10 * SAMPLE_RATE / 1000) * 2
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull_frames()
    next(gen)
    # One request covering ¶1 + the whole gap + ¶2, plus silence padding:
    got = gen.send(230)  # 230 frames = 460 bytes
    expected = (b"\x01\x02" * 2) + bytes(gap_bytes) + (b"\x03\x04" * 2)
    assert got.startswith(expected)
    assert got[len(expected) :].strip(b"\x00") == b""


def test_pull_frames_protocol(tmp_path: Path) -> None:
    """The device sends a frame count and must get EXACTLY that many
    frames back — even when a request spans a paragraph boundary, and
    padded with silence once idle."""
    first = make_wav(tmp_path, "a.raw", b"\x01\x02" * 3)  # 3 frames
    second = make_wav(tmp_path, "b.raw", b"\x03\x04" * 2)  # 2 frames
    engine = RawEngine(chunk_bytes=2)
    engine.play(0, first)
    engine.prime(1, second)

    gen = engine._pull_frames()
    assert next(gen) == b""  # prime
    assert gen.send(2) == b"\x01\x02\x01\x02"  # ¶1 frames 0-1
    assert gen.send(3) == b"\x01\x02\x03\x04\x03\x04"  # ¶1 frame 2 + ¶2
    assert gen.send(2) == b"\x00\x00\x00\x00"  # idle: silence
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
    # One request larger than both paragraphs combined: seamless join.
    assert gen.send(5) == (b"\x01\x02" * 3) + (b"\x03\x04" * 2)
    assert gen.send(2) == bytes(4)
    assert events(engine) == [
        StreamFinished(0, True),
        StreamChained(1),
        StreamFinished(1, False),
    ]


def test_miniaudio_real_device(tmp_path: Path) -> None:
    """Play 0.2 s of real audio through the actual output device."""
    if not HAVE_MINIAUDIO:
        pytest.skip("miniaudio not installed")
    import time as time_mod

    path = tmp_path / "beep.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 4410)  # 0.2 s of silence

    engine = MiniaudioEngine()
    engine.play(0, path)
    deadline = time_mod.monotonic() + 5
    while engine.current_index() is not None and time_mod.monotonic() < deadline:
        time_mod.sleep(0.02)
    engine.close()
    assert engine.current_index() is None  # finished streaming


def test_device_format_follows_file_header(tmp_path: Path) -> None:
    """Regression for the quality bug: the device must open at the cached
    files' rate (no HAL resampling), not a hardcoded constant."""
    if not HAVE_MINIAUDIO:
        pytest.skip("miniaudio not installed")

    path = tmp_path / "hi.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * 4800)  # 0.1 s of silence

    engine = MiniaudioEngine()
    engine.play(0, path)
    rate = engine._device.sample_rate
    engine.close()
    assert rate == 48000


def test_miniaudio_plays_in_real_time(tmp_path: Path) -> None:
    """Regression: the engine must answer the device's frame requests
    exactly.  A short answer stretches playback (it once played ~8x slow),
    so a 2 s stream must drain in roughly 2 s of wall time."""
    if not HAVE_MINIAUDIO:
        pytest.skip("miniaudio not installed")
    import time as time_mod

    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 22050 * 2)  # 2 s of silence

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
