"""Command line: argument parsing and the program's outermost edge.

Reading the document, probing the audio device, creating the cache —
all effects happen here or in open_app; everything after them is pure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .app import config_from_args, open_app
from .pure import split_paragraphs


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="t2s",
        description="Read a document aloud with macOS say(1), paragraph by "
        "paragraph (space: pause/replay, n/p: skip, q: quit). "
        "Paragraphs are synthesized ahead to a cache and played "
        "gaplessly in-process.",
    )
    p.add_argument(
        "file",
        nargs="?",
        default="-",
        help="text file to read ('-' or omitted: standard input)",
    )
    p.add_argument("-v", "--voice", help="voice name passed to say")
    p.add_argument(
        "-r", "--rate", type=int, metavar="WPM", help="speech rate in words per minute"
    )
    p.add_argument(
        "--width",
        type=int,
        default=72,
        metavar="COLS",
        help="display wrap width (default: 72)",
    )
    p.add_argument(
        "--start",
        type=int,
        default=1,
        metavar="N",
        help="paragraph number to start from (1-based)",
    )
    p.add_argument(
        "--split-long",
        type=int,
        default=None,
        metavar="CHARS",
        help="also split paragraphs longer than CHARS at sentence boundaries",
    )
    p.add_argument(
        "--ahead",
        type=int,
        default=3,
        metavar="N",
        help="paragraphs to synthesize ahead of playback "
        "(default: 3; raise for slow premium voices)",
    )
    p.add_argument(
        "--cache-dir",
        default=None,
        metavar="PATH",
        help="audio cache directory (default: ~/Library/Caches/t2s)",
    )
    p.add_argument(
        "--cache-limit-mb",
        type=float,
        default=256.0,
        metavar="MB",
        help="prune the cache when larger than this (default: 256; 0 = unlimited)",
    )
    p.add_argument(
        "--say-bin",
        default=None,
        metavar="PATH",
        help="say binary to run (default: say, or $T2S_SAY_BIN); "
        "useful for testing with a fake",
    )
    p.add_argument(
        "--play-bin",
        default=None,
        metavar="PATH",
        help="audio player binary (default: afplay, or $T2S_PLAY_BIN)",
    )
    p.add_argument(
        "--gap",
        type=int,
        default=0,
        metavar="MS",
        help="silence between paragraphs in milliseconds (default: 0)",
    )
    p.add_argument(
        "--data-format",
        default=None,
        metavar="FMT",
        help="synthesis format for say, e.g. LEI16@48000 "
        "(default: LEI16 at the output device's native rate)",
    )
    p.add_argument(
        "--player",
        default="auto",
        choices=["auto", "miniaudio", "afplay", "test"],
        help="playback engine: miniaudio (gapless in-process "
        "streaming), afplay (external process fallback), "
        "test (headless, for t2s's own tests), or auto "
        "(default: miniaudio, falls back to afplay)",
    )
    p.add_argument("--version", action="version", version=__version__)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.file == "-":
        if sys.stdin.isatty():
            print(
                "t2s: no input — pass a file path or pipe text (see t2s --help)",
                file=sys.stderr,
            )
            return 2
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"t2s: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2

    paras = split_paragraphs(text, args.split_long)
    if not paras:
        print("t2s: input contains no text", file=sys.stderr)
        return 1
    if not 1 <= args.start <= len(paras):
        print(
            f"t2s: --start {args.start} is out of range "
            f"(document has {len(paras)} paragraphs)",
            file=sys.stderr,
        )
        return 2

    try:
        return open_app(config_from_args(args, paras)).run()
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
