from t2s.pure import (
    AppState,
    ClearFailure,
    Note,
    PlayMode,
    StopStream,
    StreamChained,
    StreamCrashed,
    StreamFinished,
    StreamStarted,
    SyncTo,
    TryPlay,
    Warn,
    advance,
    apply_play_outcome,
    done_message,
    handle_engine_event,
    handle_key,
)


def state(
    index: int = 0,
    n_paragraphs: int = 3,
    mode: PlayMode = "stopped",
    running: bool = True,
    had_errors: bool = False,
    interactive: bool = True,
) -> AppState:
    return AppState(
        index=index,
        n_paragraphs=n_paragraphs,
        mode=mode,
        running=running,
        had_errors=had_errors,
        interactive=interactive,
    )


def test_q_quits_with_position_note() -> None:
    next_state, effects = handle_key(
        state(index=1, n_paragraphs=5, mode="playing"), "q"
    )
    assert next_state == state(index=1, n_paragraphs=5, mode="playing", running=False)
    assert effects == (Note("· stopped at ¶ 2/5"),)


def test_ctrl_c_quits_like_q() -> None:
    next_state, _ = handle_key(state(mode="playing"), "\x03")
    assert next_state == state(mode="playing", running=False)


def test_space_pauses_a_running_stream() -> None:
    next_state, effects = handle_key(state(mode="playing"), " ", engine_index=0)
    assert next_state == state(mode="paused")
    assert effects == (
        StopStream(),
        Note("⏸ paused — space: replay paragraph · n/p: paragraph · q: quit"),
    )


def test_space_while_playing_but_idle_is_a_no_op() -> None:
    next_state, effects = handle_key(state(mode="playing"), " ", engine_index=None)
    assert next_state == state(mode="playing")
    assert effects == ()


def test_space_while_paused_replays_from_the_top() -> None:
    next_state, effects = handle_key(state(index=2, n_paragraphs=5, mode="paused"), " ")
    assert next_state == state(index=2, n_paragraphs=5, mode="paused")
    assert effects == (ClearFailure(2), TryPlay(2, "· resumed"))


def test_space_while_stopped_is_a_no_op() -> None:
    next_state, effects = handle_key(state(), " ")
    assert next_state == state()
    assert effects == ()


def test_n_advances_via_tryplay() -> None:
    next_state, effects = handle_key(state(mode="playing"), "n")
    assert next_state == state(mode="playing")
    assert effects == (TryPlay(1),)


def test_p_rewinds() -> None:
    _, effects = handle_key(state(index=2, n_paragraphs=3, mode="playing"), "p")
    assert effects == (TryPlay(1),)


def test_n_at_last_paragraph_reports_the_edge() -> None:
    next_state, effects = handle_key(state(index=2, n_paragraphs=3), "n")
    assert next_state == state(index=2, n_paragraphs=3)
    assert effects == (Note("· already at last paragraph"),)


def test_p_at_first_paragraph_reports_the_edge() -> None:
    _, effects = handle_key(state(index=0, n_paragraphs=3), "p")
    assert effects == (Note("· already at first paragraph"),)


def test_unknown_key_is_ignored() -> None:
    next_state, effects = handle_key(state(mode="playing"), "x")
    assert next_state == state(mode="playing")
    assert effects == ()


def test_finished_mid_document_advances() -> None:
    next_state, effects = handle_engine_event(
        state(mode="playing"), StreamFinished(0, False)
    )
    assert next_state == state(mode="playing")
    assert effects == (TryPlay(1),)


def test_finished_chained_defers_to_the_chained_event() -> None:
    next_state, effects = handle_engine_event(
        state(mode="playing"), StreamFinished(0, True)
    )
    assert next_state.index == 0
    assert effects == ()


def test_chained_event_syncs_the_display() -> None:
    next_state, effects = handle_engine_event(state(mode="playing"), StreamChained(1))
    assert next_state == state(index=1, mode="playing")
    assert effects == (SyncTo(1),)


def test_chained_event_while_not_playing_is_ignored() -> None:
    for mode in ("paused", "stopped"):
        next_state, effects = handle_engine_event(state(mode=mode), StreamChained(1))
        assert next_state == state(mode=mode)
        assert effects == ()


def test_finished_last_paragraph_finishes_the_document() -> None:
    next_state, effects = handle_engine_event(
        state(index=2, mode="playing"), StreamFinished(2, False)
    )
    assert next_state == state(index=2, mode="playing", running=False)
    assert effects == (Note("✓ done — 3 paragraphs"),)


def test_finished_after_errors_reports_them() -> None:
    _, effects = handle_engine_event(
        state(n_paragraphs=1, mode="playing", had_errors=True),
        StreamFinished(0, False),
    )
    assert effects == (Note("✓ done (with errors)"),)


def test_finished_while_paused_is_ignored() -> None:
    next_state, effects = handle_engine_event(
        state(mode="paused"), StreamFinished(0, False)
    )
    assert next_state == state(mode="paused")
    assert effects == ()


def test_crash_while_interactive_pauses_for_space() -> None:
    next_state, effects = handle_engine_event(
        state(mode="playing", interactive=True), StreamCrashed(1, "device gone")
    )
    assert next_state == state(mode="paused", had_errors=True)
    assert effects == (
        Warn("! playback failed: device gone"),
        Note("⏸ device error — space: replay · n/p: skip · q: quit"),
    )


def test_crash_noninteractive_skips_forward() -> None:
    next_state, effects = handle_engine_event(
        state(index=1, mode="playing", interactive=False), StreamCrashed(1, "boom")
    )
    assert next_state == state(index=1, had_errors=True, interactive=False)
    assert effects == (
        Warn("! playback failed: boom — continuing with next paragraph"),
        TryPlay(2),
    )


def test_crash_while_paused_is_ignored() -> None:
    next_state, effects = handle_engine_event(
        state(mode="paused"), StreamCrashed(0, "boom")
    )
    assert next_state == state(mode="paused")
    assert effects == ()


def test_started_events_never_reach_the_application() -> None:
    next_state, effects = handle_engine_event(state(mode="playing"), StreamStarted(0))
    assert next_state == state(mode="playing")
    assert effects == ()


def test_apply_play_outcome_playing_commits() -> None:
    next_state, effects = apply_play_outcome(state(), 1, "playing")
    assert next_state == state(index=1, mode="playing")
    assert effects == ()


def test_apply_play_outcome_paused_waits_for_the_user() -> None:
    next_state, effects = apply_play_outcome(state(), 1, "paused")
    assert next_state == state(index=1, mode="paused", had_errors=True)
    assert effects == ()


def test_apply_play_outcome_failed_skips_to_next() -> None:
    next_state, effects = apply_play_outcome(state(), 0, "failed")
    assert next_state == state(had_errors=True)
    assert effects == (TryPlay(1),)


def test_apply_play_outcome_failed_on_last_finishes_with_errors() -> None:
    next_state, effects = apply_play_outcome(state(n_paragraphs=1), 0, "failed")
    assert next_state == state(n_paragraphs=1, running=False, had_errors=True)
    assert effects == (Note("✓ done (with errors)"),)


def test_skip_chain_is_loop_free() -> None:
    next_state, effects = apply_play_outcome(state(n_paragraphs=4), 0, "failed")
    tried: list[int] = []
    while effects and isinstance(effects[0], TryPlay):
        tried.append(effects[0].index)
        next_state, effects = apply_play_outcome(next_state, effects[0].index, "failed")
    assert tried == [1, 2, 3]
    assert effects == (Note("✓ done (with errors)"),)
    assert not next_state.running
    assert next_state.had_errors


def test_advance_from_a_stopped_state_tries_the_next_paragraph() -> None:
    next_state, effects = advance(state())
    assert next_state == state()
    assert effects == (TryPlay(1),)


def test_advance_at_the_end_finishes() -> None:
    next_state, effects = advance(state(index=2))
    assert next_state == state(index=2, running=False)
    assert effects == (Note("✓ done — 3 paragraphs"),)


def test_done_message_singular_plural_and_errors() -> None:
    assert done_message(1, False) == "✓ done — 1 paragraph"
    assert done_message(2, False) == "✓ done — 2 paragraphs"
    assert done_message(7, True) == "✓ done (with errors)"
