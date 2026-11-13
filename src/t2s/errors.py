from dataclasses import dataclass
from typing import final


@final
class AudioDecodeError(RuntimeError):
    pass


@final
@dataclass(frozen=True, slots=True)
class AudioLoadError:
    file_name: str
    exception_type: str
    detail: str


@final
@dataclass(frozen=True, slots=True)
class PlayerExitError:
    exit_code: int
    standard_error: str


@final
@dataclass(frozen=True, slots=True)
class SimulatedDeviceError:
    pass


type PlaybackError = AudioLoadError | PlayerExitError | SimulatedDeviceError


@final
@dataclass(frozen=True, slots=True)
class SaySynthesisError:
    exit_code: int
    standard_error: str


@final
@dataclass(frozen=True, slots=True)
class PhononSynthesisError(Exception):
    detail: str


@final
@dataclass(frozen=True, slots=True)
class SynthesisShutdown:
    pass


type SynthesisError = SaySynthesisError | PhononSynthesisError | SynthesisShutdown


@final
@dataclass(frozen=True, slots=True)
class StartupError:
    message: str
    exit_code: int
