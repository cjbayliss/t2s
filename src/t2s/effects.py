import sys
import time
from pathlib import Path

from t2s.monads import IO, io


def print_line(text: str) -> IO[None]:
    def printed() -> None:
        print(text)
        sys.stdout.flush()

    return io(printed)


def print_error(text: str) -> IO[None]:
    def printed() -> None:
        print(text, file=sys.stderr)
        sys.stderr.flush()

    return io(printed)


def sleep(seconds: float) -> IO[None]:
    def slept() -> None:
        time.sleep(seconds)

    return io(slept)


def path_exists(path: Path) -> IO[bool]:
    def probed() -> bool:
        return path.exists()

    return io(probed)
