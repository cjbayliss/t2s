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

SAMPLE_RATE = 22050
CHANNELS = 1
FORMAT_NAME = "LEI16@22050"


def nominal_output_rate(raw: int) -> int:
    return raw if 8000 <= raw <= 384000 else 48000


def frames_to_bytes(frames: int, channels: int) -> int:
    return frames * channels * 2


def gap_bytes(gap_ms: int, rate: int, channels: int) -> int:
    return frames_to_bytes(int(round(gap_ms * rate / 1000)), channels)


_SENTENCE_RE = re.compile(r"[^.!?…]*[.!?…]+[\"'”’)\]]*(?:\s+|$)|[^.!?…]+$")


def normalize(text: str) -> str:
    return " ".join(text.split())


def split_sentences(text: str) -> tuple[str, ...]:
    return tuple(
        m.group(0).strip() for m in _SENTENCE_RE.finditer(text) if m.group(0).strip()
    )


def pack_sentences(sentences: Sequence[str], max_chars: int) -> tuple[str, ...]:

    def pack(chunks: tuple[str, ...], sentence: str) -> tuple[str, ...]:
        if not chunks:
            return (sentence,)
        cur = chunks[-1]
        if len(cur) + 1 + len(sentence) > max_chars:
            return (*chunks, sentence)
        return (*chunks[:-1], f"{cur} {sentence}")

    return reduce(pack, sentences, ())


def split_long_paragraph(text: str, max_chars: int) -> tuple[str, ...]:
    if max_chars <= 0:
        return (text,)
    return pack_sentences(split_sentences(text), max_chars) or (text,)


def split_paragraphs(text: str, max_chars: int | None = None) -> tuple[str, ...]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = (normalize(chunk) for chunk in re.split(r"\n[ \t]*\n+", text))
    expanded = (
        split_long_paragraph(para, max_chars) if max_chars else (para,)
        for para in normalized
        if para
    )
    return tuple(chain.from_iterable(expanded))


def _line_end(text: str, start: int, width: int) -> int:
    end = min(start + width, len(text))
    if end < len(text):
        sp = text.rfind(" ", start, end + 1)
        if sp > start:
            end = sp
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
    text: str, voice: str | None, rate: int | None, data_format: str = FORMAT_NAME
) -> str:
    material = f"{voice or ''}|{rate or ''}|{data_format}|{text}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheFile:
    path: Path
    size: int
    mtime: float


def evictions(files: Sequence[CacheFile], limit_bytes: float) -> tuple[Path, ...]:
    ordered = sorted(files, key=attrgetter("mtime"))
    total = sum(f.size for f in ordered)
    remaining = (
        total - gone for gone in accumulate(chain((0,), (f.size for f in ordered)))
    )
    n = sum(1 for left in remaining if left > limit_bytes)
    return tuple(f.path for f in ordered[:n])


@dataclass(frozen=True)
class StreamState:
    sources: Mapping[int, bytes] = field(default_factory=dict[int, bytes])
    paths: Mapping[int, Path] = field(default_factory=dict[int, Path])
    cur: int | None = None
    data: bytes = b""
    pos: int = 0
    chained: int | None = None
    gap_left: int = 0
    starts: int = 0


def stream_gc(s: StreamState) -> StreamState:
    if s.cur is None and s.chained is None:
        return s
    floor = min(i for i in (s.cur, s.chained) if i is not None)
    return replace(
        s,
        sources={i: b for i, b in s.sources.items() if i >= floor},
        paths={i: p for i, p in s.paths.items() if i >= floor},
    )


def stream_play(s: StreamState, idx: int, path: Path, data: bytes) -> StreamState:
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
    chained = idx if s.cur is not None and idx == s.cur + 1 else s.chained
    return replace(
        s,
        sources={**s.sources, idx: data},
        paths={**s.paths, idx: path},
        chained=chained,
    )


def stream_stop(s: StreamState) -> StreamState:
    return stream_gc(replace(s, cur=None, data=b"", pos=0, chained=None, gap_left=0))


@dataclass(frozen=True)
class StreamStarted:
    idx: int


@dataclass(frozen=True)
class StreamFinished:
    idx: int
    chained: bool


@dataclass(frozen=True)
class StreamChained:
    idx: int


@dataclass(frozen=True)
class StreamCrashed:
    idx: int
    detail: str


EngineEvent = StreamStarted | StreamFinished | StreamChained | StreamCrashed


def stream_next_chunk(
    s: StreamState,
    want: int,
    gap_bytes: int,
    fail_at: int = 0,
) -> tuple[bytes | None, StreamState, tuple[EngineEvent, ...]]:
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


PlayState = Literal["playing", "paused", "stopped"]
PlayOutcome = Literal["playing", "paused", "failed"]


@dataclass(frozen=True)
class AppState:
    idx: int
    n_paras: int
    mode: PlayState = "stopped"
    running: bool = True
    had_errors: bool = False
    interactive: bool = False


@dataclass(frozen=True)
class TryPlay:
    idx: int
    note: str | None = None


@dataclass(frozen=True)
class StopStream:
    pass


@dataclass(frozen=True)
class ClearFailure:
    idx: int


@dataclass(frozen=True)
class SyncTo:
    idx: int


@dataclass(frozen=True)
class Note:
    text: str


@dataclass(frozen=True)
class Warn:
    text: str


Effect = TryPlay | StopStream | ClearFailure | SyncTo | Note | Warn
Effects = tuple[Effect, ...]


def handle_key(
    s: AppState, ch: str, engine_cur: int | None = None
) -> tuple[AppState, Effects]:
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
            if chained:
                return s2, ()
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
            return s, ()


def play_resolved(
    s: AppState, idx: int, outcome: PlayOutcome
) -> tuple[AppState, Effects]:
    if outcome == "playing":
        return replace(s, idx=idx, mode="playing"), ()
    if outcome == "paused":
        return replace(s, idx=idx, mode="paused", had_errors=True), ()
    return advance(replace(s, idx=idx, mode="stopped", had_errors=True))


def advance(s: AppState, *, after_error: bool = False) -> tuple[AppState, Effects]:
    nxt = s.idx + 1
    if nxt < s.n_paras:
        return s, (TryPlay(nxt),)
    return replace(s, running=False), (
        Note(done_message(s.n_paras, after_error or s.had_errors)),
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
        return "✓ done (with errors)"
    return f"✓ done — {count} paragraph{'s' if count != 1 else ''}"


def prefetch_window(cursor: int, ahead: int, n: int) -> tuple[int, ...]:
    return tuple(range(cursor, min(cursor + ahead + 1, n)))


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
