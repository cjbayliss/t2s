import sys
from pathlib import Path

from t2s.engines import make_process_engine, process_engine_play
from t2s.errors import PlayerExitError
from t2s.monads import io_run
from t2s.pure import StreamCrashed, StreamFinished

FLOOD = 200000


def test_stderr_flood_does_not_block_crash_report(tmp_path: Path) -> None:
    script = f"import sys; sys.stderr.write('x' * {FLOOD}); sys.exit(3)"
    engine = io_run(make_process_engine((sys.executable, "-c", script)))
    io_run(process_engine_play(engine, 0, tmp_path / "track.wav"))
    event = engine.events.get(timeout=10)
    assert event == StreamCrashed(
        0, PlayerExitError(exit_code=3, standard_error="x" * FLOOD)
    )


def test_stderr_flood_does_not_block_clean_finish(tmp_path: Path) -> None:
    script = f"import sys; sys.stderr.write('x' * {FLOOD}); sys.exit(0)"
    engine = io_run(make_process_engine((sys.executable, "-c", script)))
    io_run(process_engine_play(engine, 0, tmp_path / "track.wav"))
    assert engine.events.get(timeout=10) == StreamFinished(0, False)
