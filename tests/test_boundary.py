from pathlib import Path

from t2s.app import Arguments, arguments_from_namespace
from t2s.cli import build_parser
from t2s.pure import (
    engine_choice,
    gap_supported,
    resolve_backend_binary,
    resolve_cache_directory,
    resolve_play_binary,
    say_command,
)


def test_arguments_from_namespace_maps_defaults_and_overrides() -> None:
    defaults = arguments_from_namespace(build_parser().parse_args([]))
    assert defaults == Arguments(
        file="-",
        backend="say",
        backend_options=(),
        backend_binary=None,
        width=72,
        start=1,
        split_long=None,
        ahead=3,
        cache_directory=None,
        cache_limit_megabytes=256.0,
        play_binary=None,
        gap_milliseconds=0,
        player="auto",
    )
    overrides = arguments_from_namespace(
        build_parser().parse_args(
            [
                "book.txt",
                "-O",
                "voice=Fred",
                "-O",
                "r=190",
                "--backend-bin",
                "/bin/say",
                "--width",
                "40",
                "--start",
                "7",
                "--split-long",
                "1200",
                "--ahead",
                "5",
                "--cache-dir",
                "/tmp/c",
                "--cache-limit-mb",
                "10.5",
                "--play-bin",
                "/bin/afplay",
                "--gap",
                "150",
                "--player",
                "afplay",
            ]
        )
    )
    assert overrides == Arguments(
        file="book.txt",
        backend="say",
        backend_options=("voice=Fred", "r=190"),
        backend_binary="/bin/say",
        width=40,
        start=7,
        split_long=1200,
        ahead=5,
        cache_directory="/tmp/c",
        cache_limit_megabytes=10.5,
        play_binary="/bin/afplay",
        gap_milliseconds=150,
        player="afplay",
    )


def test_resolve_backend_binary_precedence() -> None:
    assert (
        resolve_backend_binary("say", "/explicit", {"T2S_BACKEND_BIN": "/environment"})
        == "/explicit"
    )
    assert (
        resolve_backend_binary("say", None, {"T2S_BACKEND_BIN": "/environment"})
        == "/environment"
    )
    assert resolve_backend_binary("say", None, {}) == "say"
    assert resolve_backend_binary("espeak", None, {}) == "espeak"


def test_resolve_play_binary_precedence() -> None:
    assert (
        resolve_play_binary("/explicit", {"T2S_PLAY_BIN": "/environment"})
        == "/explicit"
    )
    assert resolve_play_binary(None, {"T2S_PLAY_BIN": "/environment"}) == "/environment"
    assert resolve_play_binary(None, {}) == "afplay"


def test_resolve_cache_directory_precedence() -> None:
    assert resolve_cache_directory("/c", Path.home()) == Path("/c")
    assert resolve_cache_directory(None, Path("/Users/me")) == Path(
        "/Users/me/Library/Caches/t2s"
    )


def test_engine_choice_matrix() -> None:
    assert engine_choice("afplay", True) == "afplay"
    assert engine_choice("afplay", False) == "afplay"
    assert engine_choice("test", False) == "test"
    assert engine_choice("miniaudio", True) == "miniaudio"
    assert engine_choice("miniaudio", False) == "missing-miniaudio"
    assert engine_choice("auto", True) == "miniaudio"
    assert engine_choice("auto", False) == "afplay-fallback"


def test_gap_support_follows_engine_choice() -> None:
    assert gap_supported("miniaudio")
    assert gap_supported("test")
    assert not gap_supported("afplay")
    assert not gap_supported("afplay-fallback")
    assert not gap_supported("missing-miniaudio")


def test_say_command_translates_options_and_appends_wave_output() -> None:
    assert say_command("say", (), "LEI16@48000") == (
        "say",
        "--file-format",
        "WAVE",
        "--data-format",
        "LEI16@48000",
    )
    assert say_command(
        "/bin/say", ("voice=Fred", "r=44", "verbose"), "LEI16@22050"
    ) == (
        "/bin/say",
        "--voice",
        "Fred",
        "-r",
        "44",
        "--verbose",
        "--file-format",
        "WAVE",
        "--data-format",
        "LEI16@22050",
    )
