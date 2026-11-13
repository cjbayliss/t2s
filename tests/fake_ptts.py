import os
import wave
from pathlib import Path

LOG_ENVIRONMENT_VARIABLE: str = "T2S_FAKE_PTTS_LOG"
FAIL_TEXT_ENVIRONMENT_VARIABLE: str = "T2S_FAKE_PTTS_FAIL_TEXT"
SAMPLE_RATE: int = 24000
DURATION_SECONDS: float = 0.5


def append_log(line: str) -> None:
    log = os.environ.get(LOG_ENVIRONMENT_VARIABLE)
    if log:
        with Path(log).open("a", encoding="utf-8") as sink:
            sink.write(f"{line}\n")


class TTS:
    def __init__(
        self,
        model: str | None = None,
        device: str | None = None,
        quant: str | None = None,
        voice: str | None = None,
        temperature: float = 0.3,
        seed: int = 4242424242424242,
        cfg_coef: float | None = None,
        eos_threshold: float | None = None,
        *,
        lang: str | None = None,
        rewrites: str | None = None,
        conditions: dict[str, str | float] | None = None,
        voice_catalog: str | None = None,
    ) -> None:
        append_log(f"load\tmodel={model}\tlang={lang}")

    def save(
        self,
        path: str,
        text: str,
        *,
        voice: str | None = None,
    ) -> float:
        fail_text = os.environ.get(FAIL_TEXT_ENVIRONMENT_VARIABLE)
        if fail_text and fail_text in text:
            append_log(f"fail\t{path}\t{text}")
            raise RuntimeError(f"phonon refused: {text}")
        with wave.open(path, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(SAMPLE_RATE)
            audio.writeframes(b"\x00\x00" * int(SAMPLE_RATE * DURATION_SECONDS))
        append_log(f"save\t{path}\t{text}")
        append_log(f"voice\t{voice}")
        return DURATION_SECONDS
