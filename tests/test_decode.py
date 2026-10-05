from hypothesis import given, settings
from hypothesis import strategies as st

from t2s.pure import decode_chunk, split_partial

CHUNKS = st.lists(st.binary(max_size=16), max_size=10)


def test_split_partial_holds_incomplete_trailing_sequence() -> None:
    assert split_partial(b"ab\xc3") == (b"ab", b"\xc3")
    assert split_partial(b"ab\xe0\x80") == (b"ab", b"\xe0\x80")
    assert split_partial(b"\xf0\x9f\x98") == (b"", b"\xf0\x9f\x98")


def test_split_partial_releases_complete_sequences() -> None:
    assert split_partial(b"ab\xc3\xa9") == (b"ab\xc3\xa9", b"")
    assert split_partial(b"\xf0\x9f\x98\x80") == (b"\xf0\x9f\x98\x80", b"")
    assert split_partial(b"abc") == (b"abc", b"")


def test_split_partial_rejects_invalid_and_empty() -> None:
    assert split_partial(b"") == (b"", b"")
    assert split_partial(b"\xff\xfe") == (b"\xff\xfe", b"")
    assert split_partial(b"\x80\x80\x80\x80") == (b"\x80\x80\x80\x80", b"")


def test_decode_chunk_passes_ascii_through() -> None:
    assert decode_chunk(b"", b"np q") == ("np q", b"")
    assert decode_chunk(b"", b"") == ("", b"")


def test_decode_chunk_drops_invalid_bytes() -> None:
    assert decode_chunk(b"", b"a\xffb") == ("ab", b"")


def test_decode_chunk_reassembles_split_multibyte_key() -> None:
    text, pending = decode_chunk(b"", b"n\xc3")
    assert text == "n"
    assert pending == b"\xc3"
    text, pending = decode_chunk(pending, b"\xa9p")
    assert text == "ép"
    assert pending == b""


def test_decode_chunk_reassembles_emoji_across_three_reads() -> None:
    text, pending = decode_chunk(b"", b"\xf0")
    assert text == "" and pending == b"\xf0"
    text, pending = decode_chunk(pending, b"\x9f")
    assert text == "" and pending == b"\xf0\x9f"
    text, pending = decode_chunk(pending, b"\x98")
    assert text == "" and pending == b"\xf0\x9f\x98"
    text, pending = decode_chunk(pending, b"\x80q")
    assert text == "\U0001f600q"
    assert pending == b""


@settings(max_examples=50)
@given(st.binary(max_size=64), st.binary(max_size=64))
def test_decode_chunk_emits_ignore_decoded_prefix(pending: bytes, data: bytes) -> None:
    text, rest = decode_chunk(pending, data)
    assert text == (pending + data).decode("utf-8", "ignore")
    assert len(rest) <= 3


@settings(max_examples=50)
@given(CHUNKS)
def test_chunked_decoding_matches_whole_input(chunks: list[bytes]) -> None:
    text = ""
    pending = b""
    for chunk in chunks:
        part, pending = decode_chunk(pending, chunk)
        text += part
    assert text == b"".join(chunks).decode("utf-8", "ignore")
