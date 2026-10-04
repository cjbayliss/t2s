from operator import attrgetter
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from t2s.pure import (
    CacheFile,
    EngineEvent,
    StreamChained,
    StreamFinished,
    StreamStarted,
    StreamState,
    cache_key,
    evictions,
    normalize,
    split_paragraphs,
    stream_next_chunk,
    stream_play,
    stream_prime,
    wrap_offsets,
)

PARAGRAPH_TEXT = st.text(alphabet="abckmorsu.,?! ", min_size=1, max_size=60).map(
    normalize
)

PARAGRAPHS = st.lists(PARAGRAPH_TEXT.filter(bool), max_size=6)


@given(PARAGRAPHS)
def test_split_paragraphs_round_trips_joined_normalized_paragraphs(
    paras: list[str],
) -> None:
    assert split_paragraphs("\n\n".join(paras)) == tuple(paras)


@given(st.text(max_size=400))
def test_split_paragraphs_yields_normalized_nonempty_paragraphs(text: str) -> None:
    paras = split_paragraphs(text)
    assert all(p and normalize(p) == p for p in paras)
    assert bool(paras) == bool(text.strip())


@given(st.text(max_size=300), st.integers(1, 40))
def test_wrapped_lines_fit_width_and_map_back(text: str, width: int) -> None:
    source = normalize(text) or "x"
    previous = -1
    for line, offset in wrap_offsets(source, width):
        assert len(line) <= width
        assert offset > previous
        assert source[offset : offset + len(line)] == line
        previous = offset


@given(
    st.lists(st.tuples(st.integers(1, 10**6), st.integers(0, 2**40)), max_size=20),
    st.integers(0, 5 * 10**6),
)
def test_evictions_take_the_oldest_prefix_until_under_budget(
    entries: list[tuple[int, int]], limit: int
) -> None:
    files = tuple(
        CacheFile(Path(f"{i}.wav"), size, float(mtime))
        for i, (size, mtime) in enumerate(entries)
    )
    evicted = evictions(files, float(limit))
    ordered = sorted(files, key=attrgetter("mtime"))
    n = len(evicted)
    assert evicted == tuple(f.path for f in ordered[:n])
    total = sum(f.size for f in files)
    remaining = total - sum(f.size for f in ordered[:n])
    assert remaining <= limit
    if total <= limit:
        assert n == 0
    if n > 0:
        assert total - sum(f.size for f in ordered[: n - 1]) > limit


@settings(max_examples=25)
@given(
    st.binary(min_size=1, max_size=64),
    st.binary(min_size=1, max_size=64),
    st.lists(st.integers(1, 50), min_size=1, max_size=30),
)
def test_stream_chunks_concatenate_to_body_plus_gap_at_any_request_sizes(
    first: bytes, second: bytes, wants: list[int]
) -> None:
    gap = 7
    s = stream_play(StreamState(), 0, Path("a"), first)
    s = stream_prime(s, 1, Path("b"), second)
    collected: list[bytes] = []
    events: list[EngineEvent] = []
    while True:
        chunk, s, evs = stream_next_chunk(s, wants[len(collected) % len(wants)], gap)
        events.extend(evs)
        if chunk is None:
            break
        collected.append(chunk)
    assert b"".join(collected) == first + bytes(gap) + second
    assert events == [
        StreamStarted(0),
        StreamFinished(0, True),
        StreamChained(1),
        StreamStarted(1),
        StreamFinished(1, False),
    ]


@settings(max_examples=25)
@given(
    st.text(max_size=60),
    st.one_of(st.none(), st.text(alphabet="abcXYZ", max_size=10)),
    st.one_of(st.none(), st.integers(60, 400)),
)
def test_cache_key_is_deterministic_and_field_sensitive(
    text: str, voice: str | None, rate: int | None
) -> None:
    key = cache_key(text, voice, rate)
    assert cache_key(text, voice, rate) == key
    assert cache_key(text + "x", voice, rate) != key
    assert cache_key(text, (voice or "") + "x", rate) != key
    assert cache_key(text, voice, (rate or 0) + 1) != key
    assert cache_key(text, voice, rate, "LEI16@999") != key
