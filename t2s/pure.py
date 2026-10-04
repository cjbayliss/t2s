"""Pure core of t2s: values in, values out.

Text splitting, display wrapping, cache policy, the stream state machine
and the application state machine all live here as value-to-value
functions and frozen dataclasses.  This module imports no effect
machinery — no threads, subprocesses, files, clocks, or environment —
so everything in it is testable by plain value assertions.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import reduce
from itertools import accumulate, chain
from operator import attrgetter
from pathlib import Path
from typing import Literal

# --------------------------------------------------------------------------- #
# Stream format                                                               #
# --------------------------------------------------------------------------- #

# Synthesis is pinned to one format per session so every cached file matches
# the single audio device opened for the whole session.  The default rate is
# the output device's native rate: resampling is then nobody's job.  (The
# 0.2/0.3 pin to 22050 made the HAL resample on every modern Mac, which was
# audible as a dull, slightly gritty rendition.)
SAMPLE_RATE = 22050  # default for tests / hermetic gap math
CHANNELS = 1
FORMAT_NAME = "LEI16@22050"  # legacy default; runtime default is detected


def nominal_output_rate(raw: int) -> int:
    """Validate a probed device rate; 48000 (modern-Mac default) on doubt."""
    return raw if 8000 <= raw <= 384000 else 48000


# --------------------------------------------------------------------------- #
# Paragraph splitting                                                         #
# --------------------------------------------------------------------------- #

_SENTENCE_RE = re.compile(r"[^.!?…]*[.!?…]+[\"'”’)\]]*(?:\s+|$)|[^.!?…]+$")


def normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces and strip ends."""
    return " ".join(text.split())


def split_sentences(text: str) -> tuple[str, ...]:
    return tuple(
        m.group(0).strip() for m in _SENTENCE_RE.finditer(text) if m.group(0).strip()
    )


def pack_sentences(sentences: Sequence[str], max_chars: int) -> tuple[str, ...]:
    """Greedily pack sentences into chunks of at most max_chars characters."""

    def pack(chunks: tuple[str, ...], sentence: str) -> tuple[str, ...]:
        if not chunks:
            return (sentence,)
        cur = chunks[-1]
        if len(cur) + 1 + len(sentence) > max_chars:
            return (*chunks, sentence)
        return (*chunks[:-1], f"{cur} {sentence}")

    return reduce(pack, sentences, ())


def split_long_paragraph(text: str, max_chars: int) -> tuple[str, ...]:
    """Break an over-long paragraph into chunks at sentence boundaries."""
    if max_chars <= 0:
        return (text,)
    return pack_sentences(split_sentences(text), max_chars) or (text,)


def split_paragraphs(text: str, max_chars: int | None = None) -> tuple[str, ...]:
    """Split a document into normalized paragraphs.

    Paragraphs are separated by blank lines.  Internal whitespace is
    collapsed to single spaces so the text renders and speaks cleanly.
    If max_chars is given, paragraphs longer than that are further split
    at sentence boundaries.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = (normalize(chunk) for chunk in re.split(r"\n[ \t]*\n+", text))
    expanded = (
        split_long_paragraph(para, max_chars) if max_chars else (para,)
        for para in normalized
        if para
    )
    return tuple(chain.from_iterable(expanded))


# --------------------------------------------------------------------------- #
# Wrapping                                                                    #
# --------------------------------------------------------------------------- #


def _line_end(text: str, start: int, width: int) -> int:
    """Index just past the wrapped line that begins at `start`."""
    end = min(start + width, len(text))
    if end < len(text):
        sp = text.rfind(" ", start, end + 1)
        if sp > start:
            end = sp
    return end


def _next_line_start(text: str, end: int) -> int:
    """Where the next line begins: one separating space is dropped."""
    return end + 1 if end < len(text) and text[end] == " " else end


def wrap_offsets(text: str, width: int) -> tuple[tuple[str, int], ...]:
    """Wrap text to width, keeping each line's offset in the original text."""

    def lines() -> Iterator[tuple[str, int]]:
        """Unfold (line, offset) pairs until the text is exhausted."""
        start = 0
        while start < len(text):
            end = _line_end(text, start, width)
            yield text[start:end], start
            start = _next_line_start(text, end)

    width = max(1, width)
    return tuple(lines()) or (("", 0),)


# --------------------------------------------------------------------------- #
# Synthesis cache policy                                                      #
# --------------------------------------------------------------------------- #


def cache_key(
    text: str, voice: str | None, rate: int | None, data_format: str = FORMAT_NAME
) -> str:
    """Stable cache filename stem for a paragraph under voice/rate/format."""
    material = f"{voice or ''}|{rate or ''}|{data_format}|{text}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheFile:
    """One cached WAV as pruning sees it: identity, weight, age."""

    path: Path
    size: int
    mtime: float


def evictions(files: Sequence[CacheFile], limit_bytes: float) -> tuple[Path, ...]:
    """Oldest-first paths whose removal brings the total under limit_bytes.

    Pure policy: no I/O, so the budget arithmetic is testable directly.
    """
    ordered = sorted(files, key=attrgetter("mtime"))
    total = sum(f.size for f in ordered)
    # Remaining total after evicting 0, 1, ... of the oldest files.
    remaining = (
        total - gone for gone in accumulate(chain((0,), (f.size for f in ordered)))
    )
    n = sum(1 for left in remaining if left > limit_bytes)
    return tuple(f.path for f in ordered[:n])


# --------------------------------------------------------------------------- #
# Stream state machine                                                        #
# --------------------------------------------------------------------------- #
#
# The default engine decodes cached WAVs into memory and streams them through
# one long-lived miniaudio device, chaining straight into the next paragraph's
# samples the moment the current ones run out — no silence between paragraphs.
# The pull generator runs on the audio callback thread and must never block:
# when there is nothing to play it yields silence and the device stays open.
#
# The stream itself is an immutable value (StreamState below).  All of its
# transitions — play, prime, stop, pull — are pure functions; ChainEngine is
# the thin effectful shell that guards them with a lock, decodes files, and
# dispatches their events.


@dataclass(frozen=True)
class StreamState:
    """Immutable snapshot of the chained sample stream.

    `sources`/`paths` hold decoded samples and their files for the live
    window; `cur`/`data`/`pos` are the paragraph being streamed; `chained`
    is a decoded next paragraph the stream may roll into; `gap_left` is
    inter-paragraph silence still owed.  `starts` counts stream starts
    across the engine's lifetime, so crash injection (`fail_at`) stays a
    pure function of the state instead of a mutating callback.
    """

    sources: Mapping[int, bytes] = field(default_factory=dict[int, bytes])
    paths: Mapping[int, Path] = field(default_factory=dict[int, Path])
    cur: int | None = None
    data: bytes = b""
    pos: int = 0
    chained: int | None = None
    gap_left: int = 0
    starts: int = 0


def stream_gc(s: StreamState) -> StreamState:
    """Drop samples/paths below the stream's lowest live paragraph."""
    if s.cur is None and s.chained is None:
        return s
    floor = min(i for i in (s.cur, s.chained) if i is not None)
    return replace(
        s,
        sources={i: b for i, b in s.sources.items() if i >= floor},
        paths={i: p for i, p in s.paths.items() if i >= floor},
    )


def stream_play(s: StreamState, idx: int, path: Path, data: bytes) -> StreamState:
    """Start streaming paragraph idx from freshly decoded samples."""
    return stream_gc(
        replace(
            s,
            sources={**s.sources, idx: data},
            paths={**s.paths, idx: path},
            cur=idx,
            data=data,
            pos=0,
            chained=None,
        )
    )


def stream_prime(s: StreamState, idx: int, path: Path, data: bytes) -> StreamState:
    """Register decoded-ahead samples; chain them if they directly follow."""
    chained = idx if s.cur is not None and idx == s.cur + 1 else s.chained
    return replace(
        s,
        sources={**s.sources, idx: data},
        paths={**s.paths, idx: path},
        chained=chained,
    )


def stream_stop(s: StreamState) -> StreamState:
    """Go idle.  The device stays open; replay starts from stream_play."""
    return stream_gc(replace(s, cur=None, data=b"", pos=0, chained=None, gap_left=0))


@dataclass(frozen=True)
class StreamStarted:
    """Streaming of idx actually began (internal: engine hook, not queued)."""

    idx: int


@dataclass(frozen=True)
class StreamFinished:
    """idx's samples ran out; chained says whether the stream rolled on."""

    idx: int
    chained: bool


@dataclass(frozen=True)
class StreamChained:
    """The stream rolled from the finished paragraph into idx."""

    idx: int


@dataclass(frozen=True)
class StreamCrashed:
    """The device died at the start of idx; detail says why."""

    idx: int
    detail: str


EngineEvent = StreamStarted | StreamFinished | StreamChained | StreamCrashed


def stream_next_chunk(
    s: StreamState,
    want: int,
    gap_bytes: int,
    fail_at: int = 0,
) -> tuple[bytes | None, StreamState, tuple[EngineEvent, ...]]:
    """Pure core of the stream pull: the next `want` bytes of audio.

    Paragraph boundaries are transparent: a single request can span the
    end of one paragraph, the configured inter-paragraph gap, and the
    start of the chained next one, so the stream is continuous at any
    request size.

    Returns (chunk, successor state, events).  `chunk` is None when the
    stream is idle.  StreamStarted is an internal event the engine
    performs as its `_on_started` hook instead of queueing it.  `fail_at`
    is the 1-based stream start that simulates a device error (0 = none).
    """
    state = s
    parts: tuple[bytes, ...] = ()
    events: tuple[EngineEvent, ...] = ()
    need = want
    while need > 0:
        if state.gap_left > 0:
            take = min(need, state.gap_left)
            parts += (b"\x00" * take,)
            state = replace(state, gap_left=state.gap_left - take)
            need -= take
            continue
        if state.cur is None:
            break
        cur = state.cur
        if state.pos >= len(state.data):
            nxt = state.chained
            chained = nxt is not None and nxt in state.sources
            events += (StreamFinished(cur, chained),)
            if nxt is None or nxt not in state.sources:
                state = stream_stop(state)
                break
            state = stream_gc(
                replace(
                    state,
                    cur=nxt,
                    data=state.sources[nxt],
                    pos=0,
                    chained=None,
                    gap_left=gap_bytes,
                )
            )
            events += (StreamChained(nxt),)
            continue
        if state.pos == 0:
            starts = state.starts + 1
            state = replace(state, starts=starts)
            if fail_at and starts == fail_at:
                events += (StreamCrashed(cur, "simulated device error"),)
                state = replace(state, cur=None, data=b"", pos=0, chained=None)
                break
            events += (StreamStarted(cur),)
        take = min(need, len(state.data) - state.pos)
        parts += (state.data[state.pos : state.pos + take],)
        state = replace(state, pos=state.pos + take)
        need -= take
    if not parts:
        return None, state, events
    return b"".join(parts), state, events


# --------------------------------------------------------------------------- #
# Application state machine                                                   #
# --------------------------------------------------------------------------- #
#
# The same deal as the stream, one level up: the interactive logic — which
# key does what, how engine events advance the document, when the run ends —
# is a pure function from (state, input) to (state, effects).  App is the
# effectful shell that gathers keypresses and events, folds them through
# these transitions, and performs the resulting effect values.


PlayState = Literal["playing", "paused", "stopped"]
PlayOutcome = Literal["playing", "paused", "failed"]


@dataclass(frozen=True)
class AppState:
    """Immutable snapshot of everything the application loop decides on.

    `idx` is the displayed paragraph (0-based); `mode` mirrors playback;
    `running` keeps the event loop alive; `had_errors` sours the final
    status line; `interactive` records whether a keyboard is attached
    (crashes pause for the user instead of skipping forward).
    """

    idx: int
    n_paras: int
    mode: PlayState = "stopped"
    running: bool = True
    had_errors: bool = False
    interactive: bool = False


@dataclass(frozen=True)
class TryPlay:
    """Play paragraph idx (`note` is shown after the paragraph header)."""

    idx: int
    note: str | None = None


@dataclass(frozen=True)
class StopStream:
    """Stop playback; the device stays open for an instant replay."""


@dataclass(frozen=True)
class ClearFailure:
    """Forget a failed synthesis so the worker tries idx again."""

    idx: int


@dataclass(frozen=True)
class SyncTo:
    """The engine chained into idx on its own — catch up the display."""

    idx: int


@dataclass(frozen=True)
class Note:
    """Print a status line to stdout."""

    text: str


@dataclass(frozen=True)
class Warn:
    """Print a problem report to stderr."""

    text: str


Effect = TryPlay | StopStream | ClearFailure | SyncTo | Note | Warn
Effects = tuple[Effect, ...]


def handle_key(
    s: AppState, ch: str, engine_cur: int | None = None
) -> tuple[AppState, Effects]:
    """Fold one keypress into the application state.

    `engine_cur` is the engine's current stream index at the moment of
    the press (a query, passed in rather than fetched): space may only
    pause a stream that is actually still running.
    """
    if ch in ("q", "Q", "\x03"):
        return replace(s, running=False), (
            Note(f"· stopped at ¶ {s.idx + 1}/{s.n_paras}"),
        )
    if ch == " ":
        if s.mode == "playing":
            if engine_cur is not None:
                return replace(s, mode="paused"), (
                    StopStream(),
                    Note(
                        "⏸ paused — space: replay paragraph · n/p: paragraph · q: quit"
                    ),
                )
            # The paragraph already finished; the next tick advances.
            return s, ()
        if s.mode == "paused":
            return s, (ClearFailure(s.idx), TryPlay(s.idx, "· resumed"))
        return s, ()
    if ch in ("n", "N", "p", "P"):
        delta = 1 if ch in ("n", "N") else -1
        target = s.idx + delta
        if not 0 <= target < s.n_paras:
            edge = "last" if delta > 0 else "first"
            return s, (Note(f"· already at {edge} paragraph"),)
        return s, (TryPlay(target),)
    return s, ()


def handle_engine_event(s: AppState, ev: EngineEvent) -> tuple[AppState, Effects]:
    """Fold one engine event into the application state.

    Advancement is driven by the event stream (FIFO, nothing can be
    missed) rather than by polling, so a paragraph that starts and
    finishes between two ticks is never replayed or skipped.
    """
    match ev:
        case StreamCrashed(idx, detail):
            if s.mode != "playing":
                return s, ()
            msg = f"! playback failed: {detail}"
            if s.interactive:
                return replace(s, mode="paused", had_errors=True), (
                    Warn(msg),
                    Note("⏸ device error — space: replay · n/p: skip · q: quit"),
                )
            s2, efs = advance(
                replace(s, mode="stopped", had_errors=True), after_error=True
            )
            return s2, (Warn(msg + " — continuing with next paragraph"), *efs)
        case StreamFinished(idx, chained):
            if s.mode != "playing":
                return s, ()
            s2 = replace(s, idx=idx)
            # `chained` is decided atomically at the stream boundary by
            # the engine, so event pile-ups (several short paragraphs
            # ending inside one tick) can never cause a replay.
            if chained:
                return s2, ()  # the StreamChained event syncs us forward
            if idx + 1 < s.n_paras:
                return s2, (TryPlay(idx + 1),)
            return replace(s2, running=False), (
                Note(done_message(s.n_paras, s2.had_errors)),
            )
        case StreamChained(idx):
            if s.mode == "playing":
                return replace(s, idx=idx), (SyncTo(idx),)
            return s, ()
        case StreamStarted():
            return s, ()  # internal: the engine's _on_started hook consumed it


def play_resolved(
    s: AppState, idx: int, outcome: PlayOutcome
) -> tuple[AppState, Effects]:
    """Fold the outcome of a TryPlay attempt back into the state.

    "playing" commits the new paragraph; "paused" (interactive synthesis
    failure) waits for the user; "failed" (non-interactive) skips ahead
    via advance — which may emit the next TryPlay.
    """
    if outcome == "playing":
        return replace(s, idx=idx, mode="playing"), ()
    if outcome == "paused":
        return replace(s, idx=idx, mode="paused", had_errors=True), ()
    return advance(replace(s, idx=idx, mode="stopped", had_errors=True))


def advance(s: AppState, *, after_error: bool = False) -> tuple[AppState, Effects]:
    """Move to the next paragraph, or finish the document.

    Loop-free: the skip chain re-enters here through play_resolved each
    time a render fails, so a document where every render fails costs no
    stack depth — the interpreter's work queue drives it.
    """
    nxt = s.idx + 1
    if nxt < s.n_paras:
        return s, (TryPlay(nxt),)
    return replace(s, running=False), (
        Note(done_message(s.n_paras, after_error or s.had_errors)),
    )


# --------------------------------------------------------------------------- #
# Small pure helpers used by the CLI / app shell                              #
# --------------------------------------------------------------------------- #


def build_say_cmd(
    say_bin: str, voice: str | None, rate: int | None, data_format: str
) -> tuple[str, ...]:
    """The say invocation that renders one paragraph (text on stdin) to WAV."""
    options = (
        ("-v", voice) if voice else (),
        ("-r", str(rate)) if rate else (),
        ("--file-format", "WAVE"),
        ("--data-format", data_format),
    )
    return (say_bin, *chain.from_iterable(options))


def done_message(count: int, had_errors: bool) -> str:
    """The final status line once the document has finished."""
    if had_errors:
        return "✓ done (with errors)"
    return f"✓ done — {count} paragraph{'s' if count != 1 else ''}"
