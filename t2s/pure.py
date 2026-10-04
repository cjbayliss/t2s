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


def nominal_output_rate(reported_rate: int) -> int:
    return reported_rate if 8000 <= reported_rate <= 384000 else 48000


def frames_to_bytes(frames: int, channels: int) -> int:
    return frames * channels * 2


def gap_bytes(gap_ms: int, rate: int, channels: int) -> int:
    return frames_to_bytes(int(round(gap_ms * rate / 1000)), channels)


def normalize(text: str) -> str:
    return " ".join(text.split())


def split_sentences(text: str) -> tuple[str, ...]:
    return tuple(
        m.group(0).strip()
        for m in re.finditer(r"[^.!?…]*[.!?…]+[\"'”’)\]]*(?:\s+|$)|[^.!?…]+$", text)
        if m.group(0).strip()
    )


def pack_sentences(sentences: Sequence[str], max_chars: int) -> tuple[str, ...]:

    def pack(packed: tuple[str, ...], sentence: str) -> tuple[str, ...]:
        if not packed:
            return (sentence,)
        current = packed[-1]
        if len(current) + 1 + len(sentence) > max_chars:
            return (*packed, sentence)
        return (*packed[:-1], f"{current} {sentence}")

    return reduce(pack, sentences, ())


def split_long_paragraph(text: str, max_chars: int) -> tuple[str, ...]:
    if max_chars <= 0:
        return (text,)
    return pack_sentences(split_sentences(text), max_chars) or (text,)


def split_paragraphs(text: str, max_chars: int | None = None) -> tuple[str, ...]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = (normalize(paragraph) for paragraph in re.split(r"\n[ \t]*\n+", text))
    expanded = (
        split_long_paragraph(paragraph, max_chars) if max_chars else (paragraph,)
        for paragraph in normalized
        if paragraph
    )
    return tuple(chain.from_iterable(expanded))


def _line_end(text: str, start: int, width: int) -> int:
    end = min(start + width, len(text))
    if end < len(text):
        space_index = text.rfind(" ", start, end + 1)
        if space_index > start:
            end = space_index
    return end


def _next_line_start(text: str, end: int) -> int:
    return end + 1 if end < len(text) and text[end] == " " else end


def wrap_offsets(text: str, width: int) -> tuple[tuple[str, int], ...]:

    def lines() -> Iterator[tuple[str, int]]:
        start = 0
        while start < len(text):
            end = _line_end(text, start, width)
            yield text[start:end], start
            start = _next_line_start(text, end)

    width = max(1, width)
    return tuple(lines()) or (("", 0),)


def cache_key(
    text: str,
    voice: str | None,
    rate: int | None,
    data_format: str = "LEI16@22050",
) -> str:
    material = f"{voice or ''}|{rate or ''}|{data_format}|{text}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheFile:
    path: Path
    size: int
    mtime: float


def evictions(cache_files: Sequence[CacheFile], limit_bytes: float) -> tuple[Path, ...]:
    ordered = sorted(cache_files, key=attrgetter("mtime"))
    total = sum(cache_file.size for cache_file in ordered)
    remaining = (
        total - cumulative_size
        for cumulative_size in accumulate(
            chain((0,), (cache_file.size for cache_file in ordered))
        )
    )
    evict_count = sum(1 for remaining_size in remaining if remaining_size > limit_bytes)
    return tuple(cache_file.path for cache_file in ordered[:evict_count])


@dataclass(frozen=True)
class StreamState:
    sources: Mapping[int, bytes] = field(default_factory=dict[int, bytes])
    paths: Mapping[int, Path] = field(default_factory=dict[int, Path])
    current_index: int | None = None
    data: bytes = b""
    data_pos: int = 0
    chained_index: int | None = None
    gap_bytes_left: int = 0
    start_count: int = 0


def stream_gc(state: StreamState) -> StreamState:
    if state.current_index is None and state.chained_index is None:
        return state
    floor = min(
        index
        for index in (state.current_index, state.chained_index)
        if index is not None
    )
    return replace(
        state,
        sources={i: b for i, b in state.sources.items() if i >= floor},
        paths={i: p for i, p in state.paths.items() if i >= floor},
    )


def stream_play(state: StreamState, index: int, path: Path, data: bytes) -> StreamState:
    return stream_gc(
        replace(
            state,
            sources={**state.sources, index: data},
            paths={**state.paths, index: path},
            current_index=index,
            data=data,
            data_pos=0,
            chained_index=None,
        )
    )


def stream_prime(
    state: StreamState, index: int, path: Path, data: bytes
) -> StreamState:
    chained_index = (
        index
        if state.current_index is not None and index == state.current_index + 1
        else state.chained_index
    )
    return replace(
        state,
        sources={**state.sources, index: data},
        paths={**state.paths, index: path},
        chained_index=chained_index,
    )


def stream_stop(state: StreamState) -> StreamState:
    return stream_gc(
        replace(
            state,
            current_index=None,
            data=b"",
            data_pos=0,
            chained_index=None,
            gap_bytes_left=0,
        )
    )


@dataclass(frozen=True)
class StreamStarted:
    index: int


@dataclass(frozen=True)
class StreamFinished:
    index: int
    chained: bool


@dataclass(frozen=True)
class StreamChained:
    index: int


@dataclass(frozen=True)
class StreamCrashed:
    index: int
    detail: str


EngineEvent = StreamStarted | StreamFinished | StreamChained | StreamCrashed


def stream_next_chunk(
    state: StreamState,
    want_bytes: int,
    gap_size_bytes: int,
    fail_at: int = 0,
) -> tuple[bytes | None, StreamState, tuple[EngineEvent, ...]]:
    parts: tuple[bytes, ...] = ()
    events: tuple[EngineEvent, ...] = ()
    remaining = want_bytes
    while remaining > 0:
        if state.gap_bytes_left > 0:
            take = min(remaining, state.gap_bytes_left)
            parts += (b"\x00" * take,)
            state = replace(state, gap_bytes_left=state.gap_bytes_left - take)
            remaining -= take
            continue
        if state.current_index is None:
            break
        index = state.current_index
        if state.data_pos >= len(state.data):
            next_index = state.chained_index
            chained = next_index is not None and next_index in state.sources
            events += (StreamFinished(index, chained),)
            if next_index is None or next_index not in state.sources:
                state = stream_stop(state)
                break
            state = stream_gc(
                replace(
                    state,
                    current_index=next_index,
                    data=state.sources[next_index],
                    data_pos=0,
                    chained_index=None,
                    gap_bytes_left=gap_size_bytes,
                )
            )
            events += (StreamChained(next_index),)
            continue
        if state.data_pos == 0:
            start_count = state.start_count + 1
            state = replace(state, start_count=start_count)
            if fail_at and start_count == fail_at:
                events += (StreamCrashed(index, "simulated device error"),)
                state = replace(
                    state,
                    current_index=None,
                    data=b"",
                    data_pos=0,
                    chained_index=None,
                )
                break
            events += (StreamStarted(index),)
        take = min(remaining, len(state.data) - state.data_pos)
        parts += (state.data[state.data_pos : state.data_pos + take],)
        state = replace(state, data_pos=state.data_pos + take)
        remaining -= take
    if not parts:
        return None, state, events
    return b"".join(parts), state, events


PlayMode = Literal["playing", "paused", "stopped"]
PlayOutcome = Literal["playing", "paused", "failed"]


@dataclass(frozen=True)
class AppState:
    index: int
    n_paragraphs: int
    mode: PlayMode = "stopped"
    running: bool = True
    had_errors: bool = False
    interactive: bool = False


@dataclass(frozen=True)
class TryPlay:
    index: int
    note: str | None = None


@dataclass(frozen=True)
class StopStream:
    pass


@dataclass(frozen=True)
class ClearFailure:
    index: int


@dataclass(frozen=True)
class SyncTo:
    index: int


@dataclass(frozen=True)
class Note:
    text: str


@dataclass(frozen=True)
class Warn:
    text: str


Effect = TryPlay | StopStream | ClearFailure | SyncTo | Note | Warn
Effects = tuple[Effect, ...]


def handle_key(
    state: AppState, key: str, engine_index: int | None = None
) -> tuple[AppState, Effects]:
    if key in ("q", "Q", "\x03"):
        return replace(state, running=False), (
            Note(f"stopped at {state.index + 1}/{state.n_paragraphs}"),
        )
    if key == " ":
        if state.mode == "playing":
            if engine_index is not None:
                return replace(state, mode="paused"), (
                    StopStream(),
                    Note("paused - space: replay paragraph, n/p: paragraph, q: quit"),
                )
            return state, ()
        if state.mode == "paused":
            return state, (
                ClearFailure(state.index),
                TryPlay(state.index, "resumed"),
            )
        return state, ()
    if key in ("n", "N", "p", "P"):
        delta = 1 if key in ("n", "N") else -1
        target = state.index + delta
        if not 0 <= target < state.n_paragraphs:
            edge = "last" if delta > 0 else "first"
            return state, (Note(f"already at {edge} paragraph"),)
        return state, (TryPlay(target),)
    return state, ()


def handle_engine_event(
    state: AppState, event: EngineEvent
) -> tuple[AppState, Effects]:
    match event:
        case StreamCrashed(index, detail):
            if state.mode != "playing":
                return state, ()
            msg = f"! playback failed: {detail}"
            if state.interactive:
                return replace(state, mode="paused", had_errors=True), (
                    Warn(msg),
                    Note("device error - space: replay, n/p: skip, q: quit"),
                )
            next_state, effects = advance(
                replace(state, mode="stopped", had_errors=True), after_error=True
            )
            return next_state, (
                Warn(msg + " - continuing with next paragraph"),
                *effects,
            )
        case StreamFinished(index, chained):
            if state.mode != "playing":
                return state, ()
            next_state = replace(state, index=index)
            if chained:
                return next_state, ()
            if index + 1 < state.n_paragraphs:
                return next_state, (TryPlay(index + 1),)
            return replace(next_state, running=False), (
                Note(done_message(state.n_paragraphs, next_state.had_errors)),
            )
        case StreamChained(index):
            if state.mode == "playing":
                return replace(state, index=index), (SyncTo(index),)
            return state, ()
        case StreamStarted():
            return state, ()


def apply_play_outcome(
    state: AppState, index: int, outcome: PlayOutcome
) -> tuple[AppState, Effects]:
    if outcome == "playing":
        return replace(state, index=index, mode="playing"), ()
    if outcome == "paused":
        return replace(state, index=index, mode="paused", had_errors=True), ()
    return advance(replace(state, index=index, mode="stopped", had_errors=True))


def advance(state: AppState, *, after_error: bool = False) -> tuple[AppState, Effects]:
    next_index = state.index + 1
    if next_index < state.n_paragraphs:
        return state, (TryPlay(next_index),)
    return replace(state, running=False), (
        Note(done_message(state.n_paragraphs, after_error or state.had_errors)),
    )


def build_say_cmd(
    say_bin: str, voice: str | None, rate: int | None, data_format: str
) -> tuple[str, ...]:
    options = (
        ("-v", voice) if voice else (),
        ("-r", str(rate)) if rate else (),
        ("--file-format", "WAVE"),
        ("--data-format", data_format),
    )
    return (say_bin, *chain.from_iterable(options))


def done_message(count: int, had_errors: bool) -> str:
    if had_errors:
        return "done (with errors)"
    return f"done - {count} paragraph{'s' if count != 1 else ''}"


def prefetch_window(cursor: int, ahead: int, count: int) -> tuple[int, ...]:
    return tuple(range(cursor, min(cursor + ahead + 1, count)))


EngineChoice = Literal[
    "miniaudio", "test", "afplay", "afplay-fallback", "missing-miniaudio"
]


def engine_choice(player: str, have_miniaudio: bool) -> EngineChoice:
    if player == "afplay":
        return "afplay"
    if player == "test":
        return "test"
    if have_miniaudio:
        return "miniaudio"
    if player == "miniaudio":
        return "missing-miniaudio"
    return "afplay-fallback"


def resolve_say_bin(explicit: str | None, env: Mapping[str, str]) -> str:
    return explicit or env.get("T2S_SAY_BIN") or "say"


def resolve_play_bin(explicit: str | None, env: Mapping[str, str]) -> str:
    return explicit or env.get("T2S_PLAY_BIN") or "afplay"


def resolve_cache_dir(explicit: str | None, home: Path) -> Path:
    return Path(explicit) if explicit else home / "Library" / "Caches" / "t2s"
