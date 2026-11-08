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
    make_reference,
    modify_reference,
    read_reference,
    result_bind_io,
    update_reference,
    write_reference,
)


def test_io_defers_effects_until_run() -> None:
    calls: list[int] = []

    def action() -> int:
        calls.append(1)
        return 2

    effect = io_bind(
        io_map(io(action), lambda value: value + 1), lambda value: io_pure(value * 2)
    )
    assert calls == []
    assert io_run(effect) == 6
    assert calls == [1]


def test_result_bind_io_transforms_success() -> None:
    result: Result[int, str] = Success(2)
    effect = result_bind_io(result, lambda value: io_pure(value * 3))
    assert io_run(effect) == Success(6)


def test_result_bind_io_preserves_failure_without_running_continuation() -> None:
    calls: list[int] = []

    def continue_with(value: int) -> IO[int]:
        calls.append(value)
        return io_pure(value)

    result: Result[int, str] = Failure("no input")
    effect = result_bind_io(result, continue_with)
    assert io_run(effect) == Failure("no input")
    assert calls == []


def test_reference_operations_read_and_replace_values() -> None:
    reference = io_run(make_reference(1))
    assert io_run(read_reference(reference)) == 1
    io_run(write_reference(reference, 2))
    io_run(modify_reference(reference, lambda value: value + 1))
    extracted = io_run(
        update_reference(reference, lambda value: (value * 10, value + 1))
    )
    assert extracted == 30
    assert io_run(read_reference(reference)) == 4
