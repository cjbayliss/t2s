from t2s.pure import wrap_offsets


def lines(text: str, width: int) -> tuple[tuple[str, int], ...]:
    return wrap_offsets(text, width)


def test_short_text_single_line() -> None:
    out = lines("Hello there.", 72)
    assert out == (("Hello there.", 0),)


def test_wraps_at_width() -> None:
    text = "Hello there. This is a short test of interactive output."
    wrapped = lines(text, 20)
    joined = " ".join(line for line, _ in wrapped)
    assert joined == text
    for line, offset in wrapped:
        assert len(line) <= 20
        assert text[offset : offset + len(line)] == line


def test_offsets_map_back() -> None:
    text = (
        "Spending each day the color of the leaves, summer whispers "
        "through every window we open."
    )
    wrapped = lines(text, 30)
    assert " ".join(line for line, _ in wrapped) == text
    for line, offset in wrapped:
        assert text[offset : offset + len(line)] == line


def test_multiple_spaces_preserved_via_offsets() -> None:
    text = "one  two   three"
    wrapped = lines(text, 7)
    for line, offset in wrapped:
        assert text[offset : offset + len(line)] == line
    assert "".join(line.replace(" ", "") for line, _ in wrapped) == text.replace(
        " ", ""
    )


def test_long_word_hard_break() -> None:
    out = lines("aaaaaaaaaaaa", 5)
    assert [line for line, _ in out] == ["aaaaa", "aaaaa", "aa"]


def test_highlight_span_straddling_hard_break_clamps() -> None:
    text = "supercalifragilistic"
    wrapped = lines(text, 10)
    first_line, offset = wrapped[0]
    span_start, span_end = 0, len(text)
    clamped_start = max(span_start - offset, 0)
    clamped_end = min(span_end - offset, len(first_line))
    assert 0 <= clamped_start < clamped_end <= len(first_line)


def test_empty_text() -> None:
    assert lines("", 72) == (("", 0),)


def test_width_floor() -> None:
    assert lines("abc", 0) == (("a", 0), ("b", 1), ("c", 2))
