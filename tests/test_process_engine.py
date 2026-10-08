import sys
from pathlib import Path

from t2s.engines import make_process_engine, process_engine_play
from t2s.pure import StreamCrashed, StreamFinished

FLOOD = 200000


def test_stderr_flood_does_not_block_crash_report(tmp_path: Path) -> None:
    script = f"import sys; sys.stderr.write('x' * {FLOOD}); sys.exit(3)"
    engine = make_process_engine((sys.executable, "-c", script))
    process_engine_play(engine, 0, tmp_path / "track.wav")
    event = engine.events.get(timeout=10)
    assert event == StreamCrashed(0, f"player exited with code 3: {'x' * 200}")


def test_stderr_flood_does_not_block_clean_finish(tmp_path: Path) -> None:
    script = f"import sys; sys.stderr.write('x' * {FLOOD}); sys.exit(0)"
    engine = make_process_engine((sys.executable, "-c", script))
    process_engine_play(engine, 0, tmp_path / "track.wav")
    assert engine.events.get(timeout=10) == StreamFinished(0, False)
