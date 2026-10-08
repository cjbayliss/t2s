import subprocess
import time
from pathlib import Path

from conftest import Fakes, engine_log_environment, run_t2s, texts_played

DOC = (
    "First paragraph has a few words.\n"
    "\n"
    "Second paragraph is here with CRASHER inside.\n"
    "\n"
    "Third and final paragraph."
)

PARAGRAPHS = [
    "First paragraph has a few words.",
    "Second paragraph is here with CRASHER inside.",
    "Third and final paragraph.",
]


def run_doc(
    fakes: Fakes,
    tmp_path: Path,
    text: str,
    *extra: str,
    environment_extra: dict[str, str] | None = None,
    player: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    doc = tmp_path / "doc.txt"
    doc.write_text(text)
    cache = tmp_path / "cache"
    arguments = [str(doc), "--cache-dir", str(cache), *extra]
    if player:
        arguments += ["--player", player]
    result = run_t2s(arguments, fakes, environment_extra=environment_extra)
    return result, cache


def test_reads_all_paragraphs_in_order(fakes: Fakes, tmp_path: Path) -> None:
    environment, play_log = engine_log_environment(tmp_path)
    result, _ = run_doc(
        fakes, tmp_path, DOC, "--player", "test", environment_extra=environment
    )
    assert result.returncode == 0, result.stderr
    for marker in ("1/3", "2/3", "3/3"):
        assert marker in result.stdout
    assert "done" in result.stdout
    assert result.stderr == ""
    assert texts_played(fakes.say_log, play_log) == PARAGRAPHS
    assert result.stdout.count("- ") >= 3


def test_afplay_fallback_smoke(fakes: Fakes, tmp_path: Path) -> None:
    result, _ = run_doc(fakes, tmp_path, DOC, "--player", "afplay")
    assert result.returncode == 0, result.stderr
    assert fakes.played_texts() == PARAGRAPHS


def test_stdin_pipeline(fakes: Fakes, tmp_path: Path) -> None:
    environment, play_log = engine_log_environment(tmp_path)
    cache = tmp_path / "cache"
    result = run_t2s(
        ["--cache-dir", str(cache), "--player", "test"],
        fakes,
        environment_extra=environment,
        input=b"Piped in.\n\nSecond piped.\n",
    )
    assert result.returncode == 0, result.stderr
    assert "1/2" in result.stdout and "2/2" in result.stdout
    assert texts_played(fakes.say_log, play_log) == ["Piped in.", "Second piped."]


def test_start_flag_skips_earlier_paragraphs(fakes: Fakes, tmp_path: Path) -> None:
    environment, play_log = engine_log_environment(tmp_path)
    result, _ = run_doc(
        fakes,
        tmp_path,
        DOC,
        "--start",
        "2",
        "--player",
        "test",
        environment_extra=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "2/3" in result.stdout
    assert "1/3" not in result.stdout
    assert texts_played(fakes.say_log, play_log) == PARAGRAPHS[1:]


def test_start_out_of_range_is_usage_error(fakes: Fakes, tmp_path: Path) -> None:
    result, _ = run_doc(fakes, tmp_path, DOC, "--start", "99")
    assert result.returncode == 2
    assert "out of range" in result.stderr


def test_missing_file_is_usage_error(fakes: Fakes) -> None:
    result = run_t2s(["/nonexistent/nope.txt", "--cache-dir", "/tmp/t2s-x"], fakes)
    assert result.returncode == 2
    assert "cannot read" in result.stderr


def test_empty_input_errors(fakes: Fakes, tmp_path: Path) -> None:
    result, _ = run_doc(fakes, tmp_path, "\n\n   \n")
    assert result.returncode == 1
    assert "no text" in result.stderr


def test_empty_stdin_errors(fakes: Fakes, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    result = run_t2s(["--cache-dir", str(cache)], fakes, input=b"")
    assert result.returncode == 1
    assert "no text" in result.stderr


def test_synthesis_failure_skips_paragraph(fakes: Fakes, tmp_path: Path) -> None:
    environment, play_log = engine_log_environment(tmp_path)
    environment = dict(environment, FAKE_SAY_FAIL_TEXT="CRASHER")
    result, _ = run_doc(
        fakes, tmp_path, DOC, "--player", "test", environment_extra=environment
    )
    assert result.returncode == 1, result.stderr
    assert "1/3" in result.stdout and "3/3" in result.stdout
    assert "could not render" in result.stderr
    assert "continuing with next paragraph" in result.stderr
    assert texts_played(fakes.say_log, play_log) == [PARAGRAPHS[0], PARAGRAPHS[2]]


def test_synthesis_failure_on_last_paragraph(fakes: Fakes, tmp_path: Path) -> None:
    environment, _ = engine_log_environment(tmp_path)
    environment = dict(environment, FAKE_SAY_FAIL_TEXT="CRASHER")
    result, _ = run_doc(
        fakes,
        tmp_path,
        "Only paragraph with CRASHER here.",
        "--player",
        "test",
        environment_extra=environment,
    )
    assert result.returncode == 1
    assert "could not render" in result.stderr
    assert "done (with errors)" in result.stdout


def test_playback_failure_continues_noninteractive(
    fakes: Fakes, tmp_path: Path
) -> None:
    environment, play_log = engine_log_environment(tmp_path, fail_at="2")
    result, _ = run_doc(
        fakes, tmp_path, DOC, "--player", "test", environment_extra=environment
    )
    assert result.returncode == 1, result.stderr
    assert "playback failed" in result.stderr
    assert "continuing with next paragraph" in result.stderr
    assert texts_played(fakes.say_log, play_log) == [PARAGRAPHS[0], PARAGRAPHS[2]]


def test_playback_failure_afplay_fallback(fakes: Fakes, tmp_path: Path) -> None:
    result, _ = run_doc(
        fakes,
        tmp_path,
        DOC,
        "--player",
        "afplay",
        environment_extra={"FAKE_PLAY_FAIL_AT": "2"},
    )
    assert result.returncode == 1, result.stderr
    assert "playback failed" in result.stderr
    assert "player exited with code 3" in result.stderr
    assert "continuing with next paragraph" in result.stderr
    assert fakes.played_texts() == PARAGRAPHS


def test_exit_code_reflects_errors(fakes: Fakes, tmp_path: Path) -> None:
    clean, _ = run_doc(fakes, tmp_path, DOC, "--player", "test")
    assert clean.returncode == 0, clean.stderr
    environment, _ = engine_log_environment(tmp_path, fail_at="1")
    failing, _ = run_doc(
        fakes, tmp_path, DOC, "--player", "test", environment_extra=environment
    )
    assert failing.returncode == 1, failing.stderr
    assert "done (with errors)" in failing.stdout


def test_corrupt_cache_file_degrades_noninteractive(
    fakes: Fakes, tmp_path: Path
) -> None:
    environment, play_log = engine_log_environment(tmp_path)
    environment = dict(environment, FAKE_SAY_CORRUPT_TEXT="CRASHER")
    result, _ = run_doc(
        fakes, tmp_path, DOC, "--player", "test", environment_extra=environment
    )
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    assert "playback failed" in result.stderr
    assert "could not load" in result.stderr
    assert "continuing with next paragraph" in result.stderr
    assert texts_played(fakes.say_log, play_log) == [PARAGRAPHS[0], PARAGRAPHS[2]]


def test_cache_reused_across_runs(fakes: Fakes, tmp_path: Path) -> None:
    doc = tmp_path / "doc.txt"
    doc.write_text(DOC)
    cache = tmp_path / "cache"
    environment, play_log = engine_log_environment(tmp_path)
    arguments = [str(doc), "--cache-dir", str(cache), "--player", "test"]

    run_t2s(arguments, fakes, environment_extra=environment)
    deadline = time.monotonic() + 5
    while (
        len(list(cache.glob("*.wav"))) < len(PARAGRAPHS) and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    renders_after_first = fakes.say_log.read_text().splitlines()

    result = run_t2s(arguments, fakes, environment_extra=environment)
    assert result.returncode == 0, result.stderr
    renders_after_second = fakes.say_log.read_text().splitlines()

    assert renders_after_second == renders_after_first
    assert texts_played(fakes.say_log, play_log) == PARAGRAPHS + PARAGRAPHS


def test_gap_option_preserves_order(fakes: Fakes, tmp_path: Path) -> None:
    environment, play_log = engine_log_environment(tmp_path)
    result, _ = run_doc(
        fakes,
        tmp_path,
        DOC,
        "--gap",
        "150",
        "--player",
        "test",
        environment_extra=environment,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert texts_played(fakes.say_log, play_log) == PARAGRAPHS


def test_gap_with_afplay_warns_but_still_reads(fakes: Fakes, tmp_path: Path) -> None:
    result, _ = run_doc(fakes, tmp_path, DOC, "--gap", "150", "--player", "afplay")
    assert result.returncode == 0, result.stderr
    assert "--gap is only supported by the miniaudio engine" in result.stderr
    assert fakes.played_texts() == PARAGRAPHS


def test_width_flag(fakes: Fakes, tmp_path: Path) -> None:
    result, _ = run_doc(
        fakes,
        tmp_path,
        "one two three four five six seven eight nine ten",
        "--width",
        "20",
        "--player",
        "test",
    )
    assert result.returncode == 0
    out_lines = [
        line
        for line in result.stdout.splitlines()
        if line and not line.startswith("- ") and "done" not in line
    ]
    assert out_lines
    assert all(len(line) <= 20 for line in out_lines)


def test_long_paragraph_display(fakes: Fakes, tmp_path: Path) -> None:
    text = (
        "The quick brown fox jumps over the lazy dog again and again "
        "while the sleepy farmer counts his sheep twice before dawn "
        "breaks over the eastern ridge and the rooster crows."
    )
    result, _ = run_doc(fakes, tmp_path, text, "--player", "test")
    assert result.returncode == 0, result.stderr
    display = [
        line
        for line in result.stdout.splitlines()
        if line and not line.startswith("- ") and "done" not in line
    ]
    assert display
    assert all(len(line) <= 72 for line in display)
    assert " ".join(display) == text


def test_split_long_flag(fakes: Fakes, tmp_path: Path) -> None:
    para = (
        "One two three four five six seven eight. "
        "Nine ten eleven twelve thirteen fourteen fifteen. "
        "Sixteen seventeen eighteen nineteen twenty."
    )
    environment, play_log = engine_log_environment(tmp_path)
    result, _ = run_doc(
        fakes,
        tmp_path,
        para + "\n\nTail paragraph.",
        "--split-long",
        "60",
        "--player",
        "test",
        environment_extra=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "1/4" in result.stdout
    assert "4/4" in result.stdout
    assert len(texts_played(fakes.say_log, play_log)) == 4
