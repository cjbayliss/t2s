import os
import sys
from pathlib import Path
from types import ModuleType

import pytest
from conftest import FakePhonon

from t2s.errors import PhononSynthesisError
from t2s.monads import Failure, Result, Success, io_run
from t2s.phonon import (
    PhononSettings,
    make_phonon_plan,
    parse_phonon_settings,
    phonon_render,
)


def unwrapped[Value, Error](result: Result[Value, Error]) -> Value:
    match result:
        case Success(value):
            return value
        case Failure(error):
            pytest.fail(f"unexpected failure: {error}")


def test_empty_options_give_defaults() -> None:
    assert unwrapped(parse_phonon_settings(())) == PhononSettings()


def test_full_valid_option_set() -> None:
    settings = unwrapped(
        parse_phonon_settings(
            (
                "model=hf://gradium/phonon-40m-fr",
                "lang=fr",
                "quant=q8",
                "device=cuda",
                "voice=Easton",
                "voice_catalog=gradium/phonon-voices",
                "rewrites=all",
                "temperature=0.55",
                "seed=7",
                "cfg_coef=1.5",
                "eos_threshold=0.9",
                "duration_delta=-0.1",
            )
        )
    )
    assert settings == PhononSettings(
        model="hf://gradium/phonon-40m-fr",
        lang="fr",
        quant="q8",
        device="cuda",
        voice="Easton",
        voice_catalog="gradium/phonon-voices",
        rewrites="all",
        temperature=0.55,
        seed=7,
        cfg_coef=1.5,
        eos_threshold=0.9,
        duration_delta=-0.1,
    )


def test_unknown_key_fails() -> None:
    match parse_phonon_settings(("nonsense=1",)):
        case Failure(error):
            assert error.exit_code == 2
            assert "unknown phonon option 'nonsense'" in error.message
            assert "known:" in error.message
        case _:
            pytest.fail("expected a failure")


@pytest.mark.parametrize(
    ("option", "expected_fragment"),
    [
        ("voice", "malformed phonon option 'voice'"),
        ("temperature=loud", "expects a number"),
        ("seed=seven", "expects an integer"),
    ],
)
def test_malformed_option_fails(option: str, expected_fragment: str) -> None:
    match parse_phonon_settings((option,)):
        case Failure(error):
            assert error.exit_code == 2
            assert expected_fragment in error.message
        case _:
            pytest.fail("expected a failure")


def test_duplicate_key_last_wins() -> None:
    settings = unwrapped(parse_phonon_settings(("voice=Amy", "voice=Easton")))
    assert settings.voice == "Easton"


def test_plan_renders_paragraphs_with_a_single_model_load(
    fake_ptts: ModuleType, fake_phonon: FakePhonon, tmp_path: Path
) -> None:
    settings = unwrapped(parse_phonon_settings(("voice=Easton",)))
    plan = io_run(make_phonon_plan(settings))
    io_run(phonon_render(plan, "first paragraph", tmp_path / "one.wav"))
    io_run(phonon_render(plan, "second paragraph", tmp_path / "two.wav"))
    assert len(fake_phonon.load_lines()) == 1
    assert len(fake_phonon.saved_lines()) == 2
    assert fake_phonon.lines().count("voice\tEaston") == 2
    for rendered in (tmp_path / "one.wav", tmp_path / "two.wav"):
        assert rendered.stat().st_size > 0


def test_binding_failure_becomes_phonon_synthesis_error(
    fake_ptts: ModuleType,
    fake_phonon: FakePhonon,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("T2S_FAKE_PTTS_FAIL_TEXT", "jinx")
    plan = io_run(make_phonon_plan(unwrapped(parse_phonon_settings(()))))
    with pytest.raises(PhononSynthesisError) as raised:
        io_run(phonon_render(plan, "a jinxed paragraph", tmp_path / "x.wav"))
    assert "phonon refused" in raised.value.detail
    assert "jinxed paragraph" in raised.value.detail


def test_missing_ptts_module_reports_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "ptts", None)
    plan = io_run(make_phonon_plan(unwrapped(parse_phonon_settings(()))))
    with pytest.raises(PhononSynthesisError) as raised:
        io_run(phonon_render(plan, "hello", tmp_path / "x.wav"))
    assert "pip install t2s[phonon]" in raised.value.detail


@pytest.mark.skipif(
    not os.environ.get("T2S_PHONON_SMOKE"),
    reason="set T2S_PHONON_SMOKE=1 to synthesize with the real ptts wheel",
)
def test_real_model_smoke(tmp_path: Path) -> None:
    settings = unwrapped(parse_phonon_settings(()))
    plan = io_run(make_phonon_plan(settings))
    io_run(phonon_render(plan, "Hello from Phonon.", tmp_path / "smoke.wav"))
    assert (tmp_path / "smoke.wav").stat().st_size > 0
