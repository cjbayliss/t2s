import argparse
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from t2s.app import (
    App,
    Arguments,
    arguments_from_namespace,
    config_from_arguments,
    open_app,
    run,
)
from t2s.effects import print_error
from t2s.errors import StartupError
from t2s.monads import (
    IO,
    Failure,
    Result,
    Success,
    io,
    io_bind,
    io_map,
    io_pure,
    io_run,
)
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
    try:
        return io_run(main_effect(arguments))
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


def main_effect(arguments: Arguments) -> IO[int]:
    return io_bind(read_document(arguments), play_document(arguments))


def read_document(arguments: Arguments) -> IO[Result[str, StartupError]]:
    def read() -> Result[str, StartupError]:
        if arguments.file == "-":
            return read_standard_input()
        try:
            return Success(
                Path(arguments.file).read_text(encoding="utf-8", errors="replace")
            )
        except OSError as exception:
            return unreadable_document(arguments.file, exception)

    return io(read)


def read_standard_input() -> Result[str, StartupError]:
    if sys.stdin.isatty():
        return Failure(
            StartupError(
                message=(
                    "t2s: no input - pass a file path or pipe text (see t2s --help)"
                ),
                exit_code=2,
            )
        )
    return Success(sys.stdin.read())


def unreadable_document(name: str, exception: OSError) -> Failure[StartupError]:
    return Failure(
        StartupError(message=f"t2s: cannot read {name}: {exception}", exit_code=2)
    )


def play_document(
    arguments: Arguments,
) -> Callable[[Result[str, StartupError]], IO[int]]:
    def continue_with(document: Result[str, StartupError]) -> IO[int]:
        match document:
            case Failure(error):
                return report_startup_error(error)
            case Success(text):
                return io_bind(prepare_app(arguments, text), launch_prepared)

    return continue_with


def prepare_app(arguments: Arguments, text: str) -> IO[Result[App, StartupError]]:
    paragraphs = split_paragraphs(text, arguments.split_long)
    if not paragraphs:
        return io_pure(
            Failure(StartupError(message="t2s: input contains no text", exit_code=1))
        )
    if not 1 <= arguments.start <= len(paragraphs):
        return io_pure(
            Failure(
                StartupError(
                    message=(
                        f"t2s: --start {arguments.start} is out of range "
                        f"(document has {len(paragraphs)} paragraphs)"
                    ),
                    exit_code=2,
                )
            )
        )
    return open_app(config_from_arguments(arguments, paragraphs))


def launch_prepared(started: Result[App, StartupError]) -> IO[int]:
    match started:
        case Failure(error):
            return report_startup_error(error)
        case Success(app):
            return run(app)


def report_startup_error(error: StartupError) -> IO[int]:
    return io_map(print_error(error.message), lambda _: error.exit_code)
