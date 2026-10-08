import argparse
from pathlib import Path

from t2s.app import arguments_from_namespace
from t2s.pure import (
    build_say_command,
    engine_choice,
    gap_supported,
    resolve_cache_directory,
    resolve_play_binary,
    resolve_say_binary,
)


def namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "file": "-",
        "voice": None,
        "rate": None,
        "width": 72,
        "start": 1,
        "split_long": None,
        "ahead": 3,
        "cache_directory": None,
        "cache_limit_megabytes": 256.0,
        "say_binary": None,
        "play_binary": None,
        "gap": 0,
        "data_format": None,
        "player": "auto",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_arguments_from_namespace_reads_every_field() -> None:
    namespace_arguments = namespace(
        file="book.txt",
        voice="Fred",
        rate=190,
        width=40,
        start=7,
        split_long=1200,
        ahead=5,
        cache_directory="/tmp/c",
        cache_limit_megabytes=10.5,
        say_binary="/bin/say",
        play_binary="/bin/afplay",
        gap=150,
        data_format="LEI16@44100",
        player="afplay",
    )
    arguments = arguments_from_namespace(namespace_arguments)
    assert (
        arguments.file,
        arguments.voice,
        arguments.rate,
        arguments.width,
        arguments.start,
        arguments.split_long,
        arguments.ahead,
        arguments.cache_directory,
        arguments.cache_limit_megabytes,
        arguments.say_binary,
        arguments.play_binary,
        arguments.gap_milliseconds,
        arguments.data_format,
        arguments.player,
    ) == (
        "book.txt",
        "Fred",
        190,
        40,
        7,
        1200,
        5,
        "/tmp/c",
        10.5,
        "/bin/say",
        "/bin/afplay",
        150,
        "LEI16@44100",
        "afplay",
    )


def test_arguments_defaults_survive_the_namespace_roundtrip() -> None:
    arguments = arguments_from_namespace(namespace())
    assert arguments.file == "-"
    assert arguments.voice is None
    assert arguments.rate is None
    assert arguments.width == 72
    assert arguments.start == 1
    assert arguments.split_long is None
    assert arguments.ahead == 3
    assert arguments.cache_directory is None
    assert arguments.cache_limit_megabytes == 256.0
    assert arguments.say_binary is None
    assert arguments.play_binary is None
    assert arguments.gap_milliseconds == 0
    assert arguments.data_format is None
    assert arguments.player == "auto"


def test_resolve_say_binary_precedence() -> None:
    assert (
        resolve_say_binary("/explicit", {"T2S_SAY_BIN": "/environment"}) == "/explicit"
    )
    assert resolve_say_binary(None, {"T2S_SAY_BIN": "/environment"}) == "/environment"
    assert resolve_say_binary(None, {}) == "say"
    assert resolve_say_binary(None, {"OTHER": "x"}) == "say"


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


def test_build_say_command_includes_only_given_options() -> None:
    assert build_say_command("say", None, None, "LEI16@48000") == (
        "say",
        "--file-format",
        "WAVE",
        "--data-format",
        "LEI16@48000",
    )
    assert build_say_command("/bin/say", "Fred", 190, "LEI16@22050") == (
        "/bin/say",
        "-v",
        "Fred",
        "-r",
        "190",
        "--file-format",
        "WAVE",
        "--data-format",
        "LEI16@22050",
    )
