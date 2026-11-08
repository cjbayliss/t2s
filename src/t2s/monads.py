import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import final


@final
@dataclass(frozen=True, slots=True)
class IO[A]:
    action: Callable[[], A]


def io[A](action: Callable[[], A]) -> IO[A]:
    return IO(action=action)


def io_pure[A](value: A) -> IO[A]:
    return IO(action=lambda: value)


def io_map[A, B](effect: IO[A], transform: Callable[[A], B]) -> IO[B]:
    def mapped() -> B:
        return transform(effect.action())

    return IO(action=mapped)


def io_bind[A, B](effect: IO[A], continue_with: Callable[[A], IO[B]]) -> IO[B]:
    def bound() -> B:
        return continue_with(effect.action()).action()

    return IO(action=bound)


def io_run[A](effect: IO[A]) -> A:
    return effect.action()


@final
@dataclass(frozen=True, slots=True)
class Success[A]:
    value: A


@final
@dataclass(frozen=True, slots=True)
class Failure[E]:
    error: E


type Result[Value, Error] = Success[Value] | Failure[Error]


def result_bind_io[A, B, E](
    result: Result[A, E], continue_with: Callable[[A], IO[B]]
) -> IO[Result[B, E]]:
    match result:
        case Success(value):
            return io_map(continue_with(value), lift_success)
        case Failure(error):
            return io_pure(Failure(error))


def lift_success[B](value: B) -> Success[B]:
    return Success(value)


@final
@dataclass(slots=True)
class Reference[A]:
    lock: threading.Lock
    value: A


def make_reference[A](initial: A) -> IO[Reference[A]]:
    def created() -> Reference[A]:
        return Reference(lock=threading.Lock(), value=initial)

    return io(created)


def read_reference[A](source: Reference[A]) -> IO[A]:
    def read() -> A:
        with source.lock:
            return source.value

    return io(read)


def write_reference[A](target: Reference[A], value: A) -> IO[None]:
    def write() -> None:
        with target.lock:
            target.value = value

    return io(write)


def modify_reference[A](target: Reference[A], update: Callable[[A], A]) -> IO[None]:
    def modify() -> None:
        with target.lock:
            target.value = update(target.value)

    return io(modify)


def update_reference[A, B](
    target: Reference[A], update: Callable[[A], tuple[B, A]]
) -> IO[B]:
    def updated() -> B:
        with target.lock:
            extracted, replacement = update(target.value)
            target.value = replacement
            return extracted

    return io(updated)
