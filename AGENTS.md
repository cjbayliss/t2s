# AGENTS.md

Guidance for humans and LLM agents editing t2s. The project follows a
**strict functional programming style**; the rules below are the review
bar for every change, and `ruff`, `mypy --strict`, and `pytest` are the
gate every change must pass.

## The rules

1. **Pure functions by default.** Same arguments in, same value out. A
   function must not perform I/O, read global state, mutate anything,
   or depend on anything that does. Values in, values out.

2. **Data is immutable.** Dataclasses are `@dataclass(frozen=True)`;
   derive new values with `dataclasses.replace` instead of assigning to
   attributes. Prefer tuples and frozensets over lists and sets. Never
   call container mutators (`.append`, `.update`, `.sort`, ...) or
   assign to subscripts — build the new collection instead.

3. **No `global`/`nonlocal`.** Module-level names are constants. State
   is threaded through parameters and return values.

4. **Errors are values.** Pure code reports failure in its return value
   (`None`, a tagged union, a `Result`-shaped value) instead of raising.
   `raise`/`except` live only at effect edges, where an exception from
   the outside world is converted into a value.

5. **Effects live at the edges.** Subprocesses (`say`, `afplay`), audio
   devices, the filesystem, the terminal, clocks, threads, and queues
   are side effects. Confine them to the program boundary (`main`) and
   the sanctioned effect machinery (the synth worker and playback
   engines). Everything else takes the world as parameters — paths,
   streams, environment, time, the subprocess runner — so pure logic
   stays testable without patching.

6. **No hidden nondeterminism.** Anything that can vary between runs
   (time, randomness, environment, device state) enters as an explicit
   parameter, never as a direct call buried in pure code.

## Tooling

Both tools are configured in `pyproject.toml`; install with
`uv sync --extra test` (or `.venv/bin/pip install -e .[test]` plus the
`dev` group).

- **ruff** — formats and lints everything (`t2s.py`, `tests/`). The rule
  set pulls in isort, pyupgrade, bugbear, comprehensions, simplify,
  return, and perflint; fixes are preferred over suppressions, and a
  `noqa` needs a reason.
- **mypy** — `strict = true` over `t2s.py` and `tests/`. Every function
  is fully annotated. The untyped `miniaudio` edge is contained with
  `Any`/`cast` and an `ignore_missing_imports` override; don't let
  `Any` spread beyond it.

## Testing

- Pure functions: plain pytest with value assertions (see
  `tests/test_split.py`, `tests/test_wrap.py`).
- Effects: drive them through injected fakes (`tests/fake_say.py`,
  `tests/fake_play.py`, the `--player test` engine) and assert on
  captured outputs. Tests never touch real audio.

## Before you finish

```sh
ruff format .
ruff check .
mypy
.venv/bin/python -m pytest -q
```

All four must pass (`ruff`/`mypy` via the project venv, or prefix with
`uv run`).
