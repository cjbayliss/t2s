"""End-to-end tests: run t2s as a subprocess with fake say (silent).

Most tests run the default --player test engine (headless, fast); a few
exercise the afplay fallback via the fake afplay binary.
"""
from conftest import Fakes, engine_log_env, run_t2s, texts_played

DOC = ("First paragraph has a few words.\n"
       "\n"
       "Second paragraph is here with CRASHER inside.\n"
       "\n"
       "Third and final paragraph.")

PARAS = ["First paragraph has a few words.",
         "Second paragraph is here with CRASHER inside.",
         "Third and final paragraph."]


def run_doc(fakes: Fakes, tmp_path, text: str, *extra: str,
            env_extra: dict[str, str] | None = None,
            player: str | None = None):
    """Write text to a temp doc, run t2s with fakes + isolated cache."""
    doc = tmp_path / "doc.txt"
    doc.write_text(text)
    cache = tmp_path / "cache"
    args = [str(doc), "--cache-dir", str(cache), *extra]
    if player:
        args += ["--player", player]
    r = run_t2s(args, fakes, env_extra=env_extra)
    return r, cache


def test_reads_all_paragraphs_in_order(fakes: Fakes, tmp_path):
    env, play_log = engine_log_env(tmp_path)
    r, _ = run_doc(fakes, tmp_path, DOC, "--player", "test", env_extra=env)
    assert r.returncode == 0, r.stderr
    for marker in ("¶ 1/3", "¶ 2/3", "¶ 3/3"):
        assert marker in r.stdout
    assert "done" in r.stdout
    assert r.stderr == ""
    assert texts_played(fakes.say_log, play_log) == PARAS
    assert r.stdout.count("── ¶") >= 3


def test_afplay_fallback_smoke(fakes: Fakes, tmp_path):
    r, _ = run_doc(fakes, tmp_path, DOC, "--player", "afplay")
    assert r.returncode == 0, r.stderr
    assert fakes.played_texts() == PARAS


def test_stdin_pipeline(fakes: Fakes, tmp_path):
    env, play_log = engine_log_env(tmp_path)
    cache = tmp_path / "cache"
    r = run_t2s(["--cache-dir", str(cache), "--player", "test"], fakes,
                env_extra=env, input="Piped in.\n\nSecond piped.\n".encode())
    assert r.returncode == 0, r.stderr
    assert "¶ 1/2" in r.stdout and "¶ 2/2" in r.stdout
    assert texts_played(fakes.say_log, play_log) == ["Piped in.",
                                                     "Second piped."]


def test_start_flag_skips_earlier_paragraphs(fakes: Fakes, tmp_path):
    env, play_log = engine_log_env(tmp_path)
    r, _ = run_doc(fakes, tmp_path, DOC, "--start", "2",
                   "--player", "test", env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "¶ 2/3" in r.stdout
    assert "¶ 1/3" not in r.stdout
    assert texts_played(fakes.say_log, play_log) == PARAS[1:]


def test_start_out_of_range_is_usage_error(fakes: Fakes, tmp_path):
    r, _ = run_doc(fakes, tmp_path, DOC, "--start", "99")
    assert r.returncode == 2
    assert "out of range" in r.stderr


def test_missing_file_is_usage_error(fakes: Fakes):
    r = run_t2s(["/nonexistent/nope.txt", "--cache-dir", "/tmp/t2s-x"],
                fakes)
    assert r.returncode == 2
    assert "cannot read" in r.stderr


def test_empty_input_errors(fakes: Fakes, tmp_path):
    r, _ = run_doc(fakes, tmp_path, "\n\n   \n")
    assert r.returncode == 1
    assert "no text" in r.stderr


def test_empty_stdin_errors(fakes: Fakes, tmp_path):
    cache = tmp_path / "cache"
    r = run_t2s(["--cache-dir", str(cache)], fakes, input=b"")
    assert r.returncode == 1
    assert "no text" in r.stderr


def test_synthesis_failure_skips_paragraph(fakes: Fakes, tmp_path):
    env, play_log = engine_log_env(tmp_path)
    env = dict(env, FAKE_SAY_FAIL_TEXT="CRASHER")
    r, _ = run_doc(fakes, tmp_path, DOC, "--player", "test", env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "¶ 1/3" in r.stdout and "¶ 3/3" in r.stdout   # skipped the middle
    assert "could not render" in r.stderr
    assert "continuing with next paragraph" in r.stderr
    assert texts_played(fakes.say_log, play_log) == [PARAS[0], PARAS[2]]


def test_synthesis_failure_on_last_paragraph(fakes: Fakes, tmp_path):
    env, _ = engine_log_env(tmp_path)
    env = dict(env, FAKE_SAY_FAIL_TEXT="CRASHER")
    r, _ = run_doc(fakes, tmp_path, "Only paragraph with CRASHER here.",
                   "--player", "test", env_extra=env)
    assert r.returncode == 0
    assert "could not render" in r.stderr
    assert "done (with errors)" in r.stdout


def test_playback_failure_continues_noninteractive(fakes: Fakes, tmp_path):
    env, play_log = engine_log_env(tmp_path, fail_at="2")
    r, _ = run_doc(fakes, tmp_path, DOC, "--player", "test", env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "playback failed" in r.stderr
    assert "continuing with next paragraph" in r.stderr
    # The failed stream never started, so it is not in the log.
    assert texts_played(fakes.say_log, play_log) == [PARAS[0], PARAS[2]]


def test_playback_failure_afplay_fallback(fakes: Fakes, tmp_path):
    r, _ = run_doc(fakes, tmp_path, DOC, "--player", "afplay",
                   env_extra={"FAKE_PLAY_FAIL_AT": "2"})
    assert r.returncode == 0, r.stderr
    assert "playback failed" in r.stderr
    assert "player exited with code 3" in r.stderr
    assert "continuing with next paragraph" in r.stderr
    assert fakes.played_texts() == PARAS  # fake afplay logs before failing


def test_cache_reused_across_runs(fakes: Fakes, tmp_path):
    doc = tmp_path / "doc.txt"
    doc.write_text(DOC)
    cache = tmp_path / "cache"
    env, play_log = engine_log_env(tmp_path)
    args = [str(doc), "--cache-dir", str(cache), "--player", "test"]

    run_t2s(args, fakes, env_extra=env)
    renders_after_first = fakes.say_log.read_text().splitlines()

    r = run_t2s(args, fakes, env_extra=env)
    assert r.returncode == 0, r.stderr
    renders_after_second = fakes.say_log.read_text().splitlines()

    assert renders_after_second == renders_after_first  # nothing re-rendered
    assert texts_played(fakes.say_log, play_log) == PARAS + PARAS


def test_gap_option_preserves_order(fakes: Fakes, tmp_path):
    env, play_log = engine_log_env(tmp_path)
    r, _ = run_doc(fakes, tmp_path, DOC, "--gap", "150",
                   "--player", "test", env_extra=env)
    assert r.returncode == 0, r.stderr
    assert texts_played(fakes.say_log, play_log) == PARAS


def test_width_flag(fakes: Fakes, tmp_path):
    r, _ = run_doc(fakes, tmp_path,
                   "one two three four five six seven eight nine ten",
                   "--width", "20", "--player", "test")
    assert r.returncode == 0
    out_lines = [ln for ln in r.stdout.splitlines()
                 if ln and "¶" not in ln and "done" not in ln]
    assert out_lines
    assert all(len(ln) <= 20 for ln in out_lines)


def test_long_paragraph_display(fakes: Fakes, tmp_path):
    text = ("The quick brown fox jumps over the lazy dog again and again "
            "while the sleepy farmer counts his sheep twice before dawn "
            "breaks over the eastern ridge and the rooster crows.")
    r, _ = run_doc(fakes, tmp_path, text, "--player", "test")
    assert r.returncode == 0, r.stderr
    display = [ln for ln in r.stdout.splitlines()
               if ln and "¶" not in ln and "done" not in ln]
    assert display
    assert all(len(ln) <= 72 for ln in display)
    assert " ".join(display) == text


def test_split_long_flag(fakes: Fakes, tmp_path):
    para = ("One two three four five six seven eight. "
            "Nine ten eleven twelve thirteen fourteen fifteen. "
            "Sixteen seventeen eighteen nineteen twenty.")  # 141 chars
    env, play_log = engine_log_env(tmp_path)
    r, _ = run_doc(fakes, tmp_path, para + "\n\nTail paragraph.",
                   "--split-long", "60", "--player", "test", env_extra=env)
    assert r.returncode == 0, r.stderr
    # 141-char para splits into 3 chunks at sentence boundaries -> 4 total.
    assert "¶ 1/4" in r.stdout
    assert "¶ 4/4" in r.stdout
    assert len(texts_played(fakes.say_log, play_log)) == 4
