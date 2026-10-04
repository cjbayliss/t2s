"""Tests for paragraph splitting and sentence packing."""

import t2s


def test_split_on_blank_lines() -> None:
    text = "First para.\n\nSecond para.\n\n\n\nThird para."
    assert t2s.split_paragraphs(text) == [
        "First para.",
        "Second para.",
        "Third para.",
    ]


def test_blank_lines_with_spaces_and_crlf() -> None:
    text = "One.\r\n   \r\nTwo.\r\n\r\n\t\nThree."
    assert t2s.split_paragraphs(text) == ["One.", "Two.", "Three."]


def test_internal_whitespace_collapsed() -> None:
    text = "Wrapped\nline one\ncontinues here.\n\nSecond\ttabbed\npara."
    assert t2s.split_paragraphs(text) == [
        "Wrapped line one continues here.",
        "Second tabbed para.",
    ]


def test_empty_paragraphs_dropped() -> None:
    assert t2s.split_paragraphs("\n\n\nOnly one.\n\n   \n") == ["Only one."]
    assert t2s.split_paragraphs("") == []


def test_no_split_long_by_default() -> None:
    text = "Aaa bbb. " * 100
    assert t2s.split_paragraphs(text) == [text.strip()]


def test_split_long_breaks_at_sentences() -> None:
    para = (
        "One two three four five six. "  # 29 chars
        "Seven eight nine ten eleven twelve. "  # 36
        "Thirteen fourteen fifteen. "
    )  # 27
    chunks = t2s.split_paragraphs(para, max_chars=40)
    # Each pair of sentences exceeds 40 chars together, so one chunk each...
    # actually s1+s2 = 29+36+1 > 40, s2+s3 = 36+27+1 > 40 -> three chunks.
    assert len(chunks) == 3
    assert all(len(c) <= 40 for c in chunks)
    assert " ".join(chunks) == para.strip()
    # Whole-document view: one paragraph in, three paragraphs out.
    assert t2s.split_paragraphs(para, max_chars=40) == chunks


def test_split_long_keeps_short_paragraph_whole() -> None:
    para = "Short. Also short."
    assert t2s.split_paragraphs(para, max_chars=100) == [para]


def test_split_long_overlong_sentence_stays_whole() -> None:
    para = "Word " * 100 + "end."  # one 505-char "sentence"
    chunks = t2s.split_paragraphs(para, max_chars=50)
    assert chunks == [para.strip()]


def test_numbering_matches_paragraph_list() -> None:
    text = "A.\n\nB.\n\nC.\n\nD."
    paras = t2s.split_paragraphs(text)
    assert len(paras) == 4
    assert paras[2] == "C."  # --start 3 must land here (index 2)
