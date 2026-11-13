from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from types import UnionType
from typing import Any, Protocol, final, get_args, get_origin, get_type_hints

from t2s.errors import PhononSynthesisError, StartupError
from t2s.monads import (
    IO,
    Failure,
    Reference,
    Result,
    Success,
    io,
    io_map,
    io_run,
    make_reference,
    read_reference,
    write_reference,
)


@final
@dataclass(frozen=True, slots=True)
class PhononSettings:
    model: str | None = None
    lang: str = "en"
    quant: str | None = None
    device: str | None = None
    voice: str | None = None
    voice_catalog: str | None = None
    rewrites: str | None = None
    temperature: float = 0.3
    seed: int = 4242424242424242
    cfg_coef: float | None = None
    eos_threshold: float | None = None
    duration_delta: float | None = None


class PhononSpeechModel(Protocol):
    def save(self, path: str, text: str, *, voice: str | None = None) -> float: ...


def known_option_names() -> str:
    return ", ".join(field.name for field in fields(PhononSettings))


def unwrap_optional(annotation: object) -> object:
    if get_origin(annotation) is UnionType:
        members = tuple(
            member for member in get_args(annotation) if member is not type(None)
        )
        if len(members) == 1:
            return members[0]
    return annotation


def unparsable_value(name: str, raw: str, expected: str) -> StartupError:
    return StartupError(
        message=f"t2s: phonon option '{name}' expects {expected}, got '{raw}'",
        exit_code=2,
    )


def parse_field_value(
    name: str, annotation: object, raw: str
) -> Result[str | float | int, StartupError]:
    unwrapped = unwrap_optional(annotation)
    if unwrapped is str:
        return Success(raw)
    if unwrapped is float:
        try:
            return Success(float(raw))
        except ValueError:
            return Failure(unparsable_value(name, raw, "a number"))
    if unwrapped is int:
        try:
            return Success(int(raw))
        except ValueError:
            return Failure(unparsable_value(name, raw, "an integer"))
    return Failure(
        StartupError(
            message=f"t2s: phonon option '{name}' has an unsupported type",
            exit_code=2,
        )
    )


def parse_phonon_settings(
    options: Sequence[str],
) -> Result[PhononSettings, StartupError]:
    annotations = get_type_hints(PhononSettings)
    # keys are dataclass field names validated just above, so the target
    # types are exactly the annotations already checked against `raw`
    overrides: dict[str, Any] = {}
    for option in options:
        key, separator, raw = option.partition("=")
        annotation = annotations.get(key)
        match annotation, separator:
            case None, _:
                return Failure(
                    StartupError(
                        message=(
                            f"t2s: unknown phonon option '{key}' "
                            f"(known: {known_option_names()})"
                        ),
                        exit_code=2,
                    )
                )
            case _, "":
                return Failure(
                    StartupError(
                        message=(
                            f"t2s: malformed phonon option '{option}' "
                            "(expected KEY=VALUE)"
                        ),
                        exit_code=2,
                    )
                )
            case _:
                match parse_field_value(key, annotation, raw):
                    case Success(value):
                        overrides[key] = value
                    case Failure(error):
                        return Failure(error)
    return Success(PhononSettings(**overrides))


def load_phonon_model(settings: PhononSettings) -> IO[PhononSpeechModel]:
    def loaded() -> PhononSpeechModel:
        try:
            import ptts
        except ImportError as error:
            raise PhononSynthesisError(
                "ptts is not installed - install it with: pip install t2s[phonon] "
                f"({error})"
            ) from error
        conditions: dict[str, str | float] | None = (
            {"duration_delta": settings.duration_delta}
            if settings.duration_delta is not None
            else None
        )
        model: PhononSpeechModel = ptts.TTS(
            settings.model,
            device=settings.device,
            quant=settings.quant,
            voice=settings.voice,
            temperature=settings.temperature,
            seed=settings.seed,
            cfg_coef=settings.cfg_coef,
            eos_threshold=settings.eos_threshold,
            lang=settings.lang,
            rewrites=settings.rewrites,
            conditions=conditions,
            voice_catalog=settings.voice_catalog,
        )
        return model

    return io(loaded)


@final
@dataclass(frozen=True, slots=True)
class PhononPlan:
    settings: PhononSettings
    model: Reference[PhononSpeechModel | None]


def make_phonon_plan(settings: PhononSettings) -> IO[PhononPlan]:
    # the annotation pins the cell type; mypy otherwise infers Reference[None]
    model: IO[Reference[PhononSpeechModel | None]] = make_reference(None)
    return io_map(
        model,
        lambda loaded_model: PhononPlan(settings=settings, model=loaded_model),
    )


def phonon_render(plan: PhononPlan, text: str, path: Path) -> IO[None]:
    def rendered() -> None:
        model = io_run(read_reference(plan.model))
        if model is None:
            model = io_run(load_phonon_model(plan.settings))
            io_run(write_reference(plan.model, model))
        try:
            model.save(str(path), text, voice=plan.settings.voice)
        except Exception as error:
            # the Rust binding raises heterogeneous exception types, so the
            # catch has to be broad; the detail keeps the original shape
            raise PhononSynthesisError(f"{type(error).__name__}: {error}") from error

    return io(rendered)
