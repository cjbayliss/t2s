"""Tests for width-aware wrapping with offset tracking."""

from t2s.pure import wrap_offsets


def lines(text: str, width: int) -> tuple[tuple[str, int], ...]:
    return wrap_offsets(text, width)


def test_short_text_single_line() -> None:
    out = lines("Hello there.", 72)
    assert out == (("Hello there.", 0),)


def test_wraps_at_width() -> None:
    text = "Hello there. This is a short test of interactive output."
    out = lines(text, 20)
    joined = " ".join(line for line, _ in out)
    assert joined == text
    for line, off in out:
        assert len(line) <= 20
        assert text[off : off + len(line)] == line


def test_offsets_map_back() -> None:
    text = (
        "Spending each day the color of the leaves, summer whispers "
        "through every window we open."
    )
    out = lines(text, 30)
    assert " ".join(line for line, _ in out) == text
    for line, off in out:
        assert text[off : off + len(line)] == line


def test_multiple_spaces_preserved_via_offsets() -> None:
    text = "one  two   three"  # irregular spacing
    out = lines(text, 7)
    for line, off in out:
        assert text[off : off + len(line)] == line
    # Non-space characters all appear exactly once across the lines.
    assert "".join(line.replace(" ", "") for line, _ in out) == text.replace(" ", "")


def test_long_word_hard_break() -> None:
    out = lines("aaaaaaaaaaaa", 5)
    assert [line for line, _ in out] == ["aaaaa", "aaaaa", "aa"]


def test_highlight_span_straddling_hard_break_clamps() -> None:
    text = "supercalifragilistic"
    out = lines(text, 10)
    # Word is hard-broken across lines 1 and 2; a span over the whole word
    # must clamp to each line's portion without raising.
    line1, off1 = out[0]
    s, e = 0, len(text)
    a = max(s - off1, 0)
    b = min(e - off1, len(line1))
    assert 0 <= a < b <= len(line1)


def test_empty_text() -> None:
    assert lines("", 72) == (("", 0),)


def test_width_floor() -> None:
    assert lines("abc", 0) == (("a", 0), ("b", 1), ("c", 2))
