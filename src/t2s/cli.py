from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

from t2s.app import arguments_from_namespace, config_from_arguments, open_app, run
from t2s.pure import split_paragraphs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="t2s",
        description="Read a document aloud with macOS say(1), paragraph by "
        "paragraph (space: pause/replay, n/p: skip, q: quit). "
        "Paragraphs are synthesized ahead to a cache and played "
        "gaplessly in-process.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        default="-",
        help="text file to read ('-' or omitted: standard input)",
    )
    parser.add_argument("-v", "--voice", help="voice name passed to say")
    parser.add_argument(
        "-r", "--rate", type=int, metavar="WPM", help="speech rate in words per minute"
    )
    parser.add_argument(
        "--width",
        type=int,
        default=72,
        metavar="COLS",
        help="display wrap width (default: 72)",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        metavar="N",
        help="paragraph number to start from (1-based)",
    )
    parser.add_argument(
        "--split-long",
        type=int,
        default=None,
        metavar="CHARS",
        help="also split paragraphs longer than CHARS at sentence boundaries",
    )
    parser.add_argument(
        "--ahead",
        type=int,
        default=3,
        metavar="N",
        help="paragraphs to synthesize ahead of playback "
        "(default: 3; raise for slow premium voices)",
    )
    parser.add_argument(
        "--cache-dir",
        dest="cache_directory",
        default=None,
        metavar="PATH",
        help="audio cache directory (default: ~/Library/Caches/t2s)",
    )
    parser.add_argument(
        "--cache-limit-mb",
        dest="cache_limit_megabytes",
        type=float,
        default=256.0,
        metavar="MB",
        help="prune the cache when larger than this (default: 256; 0 = unlimited)",
    )
    parser.add_argument(
        "--say-bin",
        dest="say_binary",
        default=None,
        metavar="PATH",
        help="say binary to run (default: say, or $T2S_SAY_BIN); "
        "useful for testing with a fake",
    )
    parser.add_argument(
        "--play-bin",
        dest="play_binary",
        default=None,
        metavar="PATH",
        help="audio player binary (default: afplay, or $T2S_PLAY_BIN)",
    )
    parser.add_argument(
        "--gap",
        type=int,
        default=0,
        metavar="MS",
        help="silence between paragraphs in milliseconds (default: 0; "
        "miniaudio engine only)",
    )
    parser.add_argument(
        "--data-format",
        default=None,
        metavar="FMT",
        help="synthesis format for say, e.g. LEI16@48000 "
        "(default: LEI16 at the output device's native rate)",
    )
    parser.add_argument(
        "--player",
        default="auto",
        choices=["auto", "miniaudio", "afplay", "test"],
        help="playback engine: miniaudio (gapless in-process "
        "streaming), afplay (external process fallback), "
        "test (headless, for t2s's own tests), or auto "
        "(default: miniaudio, falls back to afplay)",
    )
    parser.add_argument("--version", action="version", version=metadata.version("t2s"))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = arguments_from_namespace(build_parser().parse_args(argv))

    if arguments.file == "-":
        if sys.stdin.isatty():
            print(
                "t2s: no input - pass a file path or pipe text (see t2s --help)",
                file=sys.stderr,
            )
            return 2
        text = sys.stdin.read()
    else:
        try:
            text = Path(arguments.file).read_text(encoding="utf-8", errors="replace")
        except OSError as exception:
            print(f"t2s: cannot read {arguments.file}: {exception}", file=sys.stderr)
            return 2

    paragraphs = split_paragraphs(text, arguments.split_long)
    if not paragraphs:
        print("t2s: input contains no text", file=sys.stderr)
        return 1
    if not 1 <= arguments.start <= len(paragraphs):
        print(
            f"t2s: --start {arguments.start} is out of range "
            f"(document has {len(paragraphs)} paragraphs)",
            file=sys.stderr,
        )
        return 2

    try:
        return run(open_app(config_from_arguments(arguments, paragraphs)))
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
