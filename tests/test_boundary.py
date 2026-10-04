import argparse
from pathlib import Path

from t2s.app import args_from_namespace
from t2s.pure import (
    build_say_cmd,
    engine_choice,
    resolve_cache_dir,
    resolve_play_bin,
    resolve_say_bin,
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
        "cache_dir": None,
        "cache_limit_mb": 256.0,
        "say_bin": None,
        "play_bin": None,
        "gap": 0,
        "data_format": None,
        "player": "auto",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_args_from_namespace_reads_every_field() -> None:
    ns = namespace(
        file="book.txt",
        voice="Fred",
        rate=190,
        width=40,
        start=7,
        split_long=1200,
        ahead=5,
        cache_dir="/tmp/c",
        cache_limit_mb=10.5,
        say_bin="/bin/say",
        play_bin="/bin/afplay",
        gap=150,
        data_format="LEI16@44100",
        player="afplay",
    )
    args = args_from_namespace(ns)
    assert (
        args.file,
        args.voice,
        args.rate,
        args.width,
        args.start,
        args.split_long,
        args.ahead,
        args.cache_dir,
        args.cache_limit_mb,
        args.say_bin,
        args.play_bin,
        args.gap,
        args.data_format,
        args.player,
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


def test_args_defaults_survive_the_namespace_roundtrip() -> None:
    args = args_from_namespace(namespace())
    assert args.file == "-"
    assert args.voice is None
    assert args.rate is None
    assert args.width == 72
    assert args.start == 1
    assert args.split_long is None
    assert args.ahead == 3
    assert args.cache_dir is None
    assert args.cache_limit_mb == 256.0
    assert args.say_bin is None
    assert args.play_bin is None
    assert args.gap == 0
    assert args.data_format is None
    assert args.player == "auto"


def test_resolve_say_bin_precedence() -> None:
    assert resolve_say_bin("/explicit", {"T2S_SAY_BIN": "/env"}) == "/explicit"
    assert resolve_say_bin(None, {"T2S_SAY_BIN": "/env"}) == "/env"
    assert resolve_say_bin(None, {}) == "say"
    assert resolve_say_bin(None, {"OTHER": "x"}) == "say"


def test_resolve_play_bin_precedence() -> None:
    assert resolve_play_bin("/explicit", {"T2S_PLAY_BIN": "/env"}) == "/explicit"
    assert resolve_play_bin(None, {"T2S_PLAY_BIN": "/env"}) == "/env"
    assert resolve_play_bin(None, {}) == "afplay"


def test_resolve_cache_dir_precedence() -> None:
    assert resolve_cache_dir("/c", Path.home()) == Path("/c")
    assert resolve_cache_dir(None, Path("/Users/me")) == Path(
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


def test_build_say_cmd_includes_only_given_options() -> None:
    assert build_say_cmd("say", None, None, "LEI16@48000") == (
        "say",
        "--file-format",
        "WAVE",
        "--data-format",
        "LEI16@48000",
    )
    assert build_say_cmd("/bin/say", "Fred", 190, "LEI16@22050") == (
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
