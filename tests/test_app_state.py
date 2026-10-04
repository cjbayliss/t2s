from t2s.pure import (
    AppState,
    ClearFailure,
    Note,
    PlayState,
    StopStream,
    StreamChained,
    StreamCrashed,
    StreamFinished,
    StreamStarted,
    SyncTo,
    TryPlay,
    Warn,
    advance,
    done_message,
    handle_engine_event,
    handle_key,
    play_resolved,
)


def s(
    idx: int = 0,
    n: int = 3,
    mode: PlayState = "stopped",
    running: bool = True,
    had_errors: bool = False,
    interactive: bool = True,
) -> AppState:
    return AppState(
        idx=idx,
        n_paras=n,
        mode=mode,
        running=running,
        had_errors=had_errors,
        interactive=interactive,
    )


def test_q_quits_with_position_note() -> None:
    st, efs = handle_key(s(idx=1, n=5, mode="playing"), "q")
    assert st == s(idx=1, n=5, mode="playing", running=False)
    assert efs == (Note("· stopped at ¶ 2/5"),)


def test_ctrl_c_quits_like_q() -> None:
    st, _ = handle_key(s(mode="playing"), "\x03")
    assert st == s(mode="playing", running=False)


def test_space_pauses_a_running_stream() -> None:
    st, efs = handle_key(s(mode="playing"), " ", engine_cur=0)
    assert st == s(mode="paused")
    assert efs == (
        StopStream(),
        Note("⏸ paused — space: replay paragraph · n/p: paragraph · q: quit"),
    )


def test_space_while_playing_but_idle_is_a_no_op() -> None:
    st, efs = handle_key(s(mode="playing"), " ", engine_cur=None)
    assert st == s(mode="playing")
    assert efs == ()


def test_space_while_paused_replays_from_the_top() -> None:
    st, efs = handle_key(s(idx=2, n=5, mode="paused"), " ")
    assert st == s(idx=2, n=5, mode="paused")
    assert efs == (ClearFailure(2), TryPlay(2, "· resumed"))


def test_space_while_stopped_is_a_no_op() -> None:
    st, efs = handle_key(s(), " ")
    assert st == s()
    assert efs == ()


def test_n_advances_via_tryplay() -> None:
    st, efs = handle_key(s(mode="playing"), "n")
    assert st == s(mode="playing")
    assert efs == (TryPlay(1),)


def test_p_rewinds() -> None:
    _, efs = handle_key(s(idx=2, n=3, mode="playing"), "p")
    assert efs == (TryPlay(1),)


def test_n_at_last_paragraph_reports_the_edge() -> None:
    st, efs = handle_key(s(idx=2, n=3), "n")
    assert st == s(idx=2, n=3)
    assert efs == (Note("· already at last paragraph"),)


def test_p_at_first_paragraph_reports_the_edge() -> None:
    _, efs = handle_key(s(idx=0, n=3), "p")
    assert efs == (Note("· already at first paragraph"),)


def test_unknown_key_is_ignored() -> None:
    st, efs = handle_key(s(mode="playing"), "x")
    assert st == s(mode="playing")
    assert efs == ()


def test_finished_mid_document_advances() -> None:
    st, efs = handle_engine_event(s(mode="playing"), StreamFinished(0, False))
    assert st == s(mode="playing")
    assert efs == (TryPlay(1),)


def test_finished_chained_defers_to_the_chained_event() -> None:
    st, efs = handle_engine_event(s(mode="playing"), StreamFinished(0, True))
    assert st.idx == 0
    assert efs == ()


def test_chained_event_syncs_the_display() -> None:
    st, efs = handle_engine_event(s(mode="playing"), StreamChained(1))
    assert st == s(idx=1, mode="playing")
    assert efs == (SyncTo(1),)


def test_chained_event_while_not_playing_is_ignored() -> None:
    for mode in ("paused", "stopped"):
        st, efs = handle_engine_event(s(mode=mode), StreamChained(1))
        assert st == s(mode=mode)
        assert efs == ()


def test_finished_last_paragraph_finishes_the_document() -> None:
    st, efs = handle_engine_event(s(idx=2, mode="playing"), StreamFinished(2, False))
    assert st == s(idx=2, mode="playing", running=False)
    assert efs == (Note("✓ done — 3 paragraphs"),)


def test_finished_after_errors_reports_them() -> None:
    _, efs = handle_engine_event(
        s(n=1, mode="playing", had_errors=True), StreamFinished(0, False)
    )
    assert efs == (Note("✓ done (with errors)"),)


def test_finished_while_paused_is_ignored() -> None:
    st, efs = handle_engine_event(s(mode="paused"), StreamFinished(0, False))
    assert st == s(mode="paused")
    assert efs == ()


def test_crash_while_interactive_pauses_for_space() -> None:
    st, efs = handle_engine_event(
        s(mode="playing", interactive=True), StreamCrashed(1, "device gone")
    )
    assert st == s(mode="paused", had_errors=True)
    assert efs == (
        Warn("! playback failed: device gone"),
        Note("⏸ device error — space: replay · n/p: skip · q: quit"),
    )


def test_crash_noninteractive_skips_forward() -> None:
    st, efs = handle_engine_event(
        s(idx=1, mode="playing", interactive=False), StreamCrashed(1, "boom")
    )
    assert st == s(idx=1, had_errors=True, interactive=False)
    assert efs == (
        Warn("! playback failed: boom — continuing with next paragraph"),
        TryPlay(2),
    )


def test_crash_while_paused_is_ignored() -> None:
    st, efs = handle_engine_event(s(mode="paused"), StreamCrashed(0, "boom"))
    assert st == s(mode="paused")
    assert efs == ()


def test_started_events_never_reach_the_application() -> None:
    st, efs = handle_engine_event(s(mode="playing"), StreamStarted(0))
    assert st == s(mode="playing")
    assert efs == ()


def test_play_resolved_playing_commits() -> None:
    st, efs = play_resolved(s(), 1, "playing")
    assert st == s(idx=1, mode="playing")
    assert efs == ()


def test_play_resolved_paused_waits_for_the_user() -> None:
    st, efs = play_resolved(s(), 1, "paused")
    assert st == s(idx=1, mode="paused", had_errors=True)
    assert efs == ()


def test_play_resolved_failed_skips_to_next() -> None:
    st, efs = play_resolved(s(), 0, "failed")
    assert st == s(had_errors=True)
    assert efs == (TryPlay(1),)


def test_play_resolved_failed_on_last_finishes_with_errors() -> None:
    st, efs = play_resolved(s(n=1), 0, "failed")
    assert st == s(n=1, running=False, had_errors=True)
    assert efs == (Note("✓ done (with errors)"),)


def test_skip_chain_is_loop_free() -> None:
    st, efs = play_resolved(s(n=4), 0, "failed")
    tried: list[int] = []
    while efs and isinstance(efs[0], TryPlay):
        tried.append(efs[0].idx)
        st, efs = play_resolved(st, efs[0].idx, "failed")
    assert tried == [1, 2, 3]
    assert efs == (Note("✓ done (with errors)"),)
    assert not st.running
    assert st.had_errors


def test_advance_from_a_stopped_state_tries_the_next_paragraph() -> None:
    st, efs = advance(s())
    assert st == s()
    assert efs == (TryPlay(1),)


def test_advance_at_the_end_finishes() -> None:
    st, efs = advance(s(idx=2))
    assert st == s(idx=2, running=False)
    assert efs == (Note("✓ done — 3 paragraphs"),)


def test_done_message_singular_plural_and_errors() -> None:
    assert done_message(1, False) == "✓ done — 1 paragraph"
    assert done_message(2, False) == "✓ done — 2 paragraphs"
    assert done_message(7, True) == "✓ done (with errors)"
