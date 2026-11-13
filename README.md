# t2s

**NOTE:** This project is created using an LLM (mostly GLM 5.3 Flash)

Read documents aloud on macOS, one paragraph at a time - gapless
in-process playback, pausable and skippable from the keyboard, with a
72-column paragraph display that survives audio device changes. The
default backend is the built-in `say(1)`; the Gradium Phonon neural
models are available in-process as an optional backend.

## Installation

Install as a standalone tool from the repository:

```sh
uv tool install git+https://github.com/cjbayliss/t2s
```

or to run it from a local clone

```sh
uv run t2s
```

For the Phonon backend, also install the `phonon` extra, which pulls in
the `ptts` Python bindings:

```sh
uv tool install git+https://github.com/cjbayliss/t2s --with ptts
```

or, inside a virtual environment:

```sh
pip install t2s[phonon]
```

## Requirements

- macOS (uses the built-in `say` and `afplay` command-line tools)
- Python 3.15 or later
- [`uv`](https://docs.astral.sh/uv/) for installation and development
- The `miniaudio` Python package for gapless playback (installed
  automatically; without it, t2s falls back to `afplay`)
- The `ptts` Python package for the optional Phonon backend (installed
  with the `phonon` extra; it ships prebuilt wheels for supported
  platforms and needs no PyTorch)

## Usage

Read a file:

```sh
t2s document.txt
```

Read from standard input:

```sh
curl -s https://example.org/article.txt | t2s
```

Keys (read from the terminal even when the text arrives on stdin):

- `space` - pause; press again to replay the current paragraph
- `n` / `p` - skip to the next / previous paragraph
- `q` or `Ctrl-C` - quit

More examples:

```sh
# Specific voice and speech rate
t2s -O voice=Daniel -O rate=190 thesis.txt

# Start from a later paragraph
t2s --start 4 long-article.txt

# Also split paragraphs longer than 1200 characters at sentence boundaries
t2s --split-long 1200 report.txt

# Gradium Phonon backend with a catalog voice and slightly faster speech
t2s -B phonon -O voice=Easton -O duration_delta=-0.1 document.txt
```

Each paragraph is synthesized ahead of playback into an audio cache and
streamed in-process, so paragraphs play without gaps. Paragraphs are
numbered from 1 in the displayed output, and `--start N` resumes from a
known position.

### Options

- `file` - text file to read; `-` or omitted reads standard input
- `-B, --backend NAME` - synthesis backend: `say` (macOS `say(1)`
  subprocess, default) or `phonon` (in-process Gradium Phonon via the
  `ptts` package)
- `-O, --option KEY=VALUE` - option passed to the backend, repeatable;
  for `say`, a single-letter key becomes a short flag (`-O r=350` ->
  `-r 350`) and anything longer a long flag (`-O rate=350` ->
  `--rate 350`); for `phonon`, keys are named settings (see
  "Phonon backend" below)
- `--width COLS` - display wrap width (default: 72)
- `--start N` - 1-based paragraph number to start from (default: 1)
- `--split-long CHARS` - also split paragraphs longer than `CHARS` at
  sentence boundaries
- `--ahead N` - paragraphs to synthesize ahead of playback (default: 3;
  raise it for slow premium voices)
- `--cache-dir PATH` - audio cache directory (default:
  `~/Library/Caches/t2s`)
- `--cache-limit-mb MB` - prune the cache when larger than this
  (default: 256; `0` means unlimited)
- `--backend-bin PATH` - backend binary to run (default: the backend's
  own binary, or `$T2S_BACKEND_BIN`); not supported by the `phonon`
  backend, which runs in-process
- `--play-bin PATH` - audio player binary (default: `afplay`, or
  `$T2S_PLAY_BIN`)
- `--gap MS` - silence between paragraphs in milliseconds (default: 0;
  miniaudio engine only)
- `--player ENGINE` - `miniaudio` (gapless in-process streaming),
  `afplay` (external process fallback), `test` (headless, used by t2s's
  own tests), or `auto` (default: miniaudio, falling back to afplay)
- `--version` - print the version and exit

### Environment variables

- `T2S_BACKEND_BIN` - fallback backend binary path when `--backend-bin`
  is not given
- `T2S_PLAY_BIN` - fallback player binary path when `--play-bin` is not
  given
- `T2S_TEST_PLAY_FAIL_AT`, `T2S_TEST_PLAY_DELAY`, `T2S_TEST_PLAY_LOG` -
  fault-injection and logging knobs for the `test` player, used by the
  test suite

Command-line flags take precedence over environment variables.

### Phonon backend

`-B phonon` synthesizes in-process with the Gradium Phonon models
through the official [`ptts`](https://pypi.org/project/ptts/) Python
bindings, producing 24 kHz mono WAVs that play through the usual
gapless engine. Install it with the `phonon` extra
(`pip install t2s[phonon]`); without `ptts`, the first paragraph reports
an error naming that command instead of playing.

Options use the same `-O KEY=VALUE` form, but the keys are the
`ptts.TTS` keyword names verbatim, not `say` flags:

| Key | Meaning | `ptts.TTS` counterpart |
| --- | --- | --- |
| `model` | model path or `hf://owner/name` | first positional argument |
| `lang` | text-normalization language (default: `en`) | `lang` |
| `quant` | weight format, e.g. `q8` | `quant` |
| `device` | compute device | `device` |
| `voice` | voice name from the checkpoint or catalog | `voice` |
| `voice_catalog` | voice catalog repo or local `voices.jsonl` | `voice_catalog` |
| `rewrites` | rewrite rule selection (`default`, `all`, `none`, ...) | `rewrites` |
| `temperature` | sampling temperature (default: `0.3`) | `temperature` |
| `seed` | sampling seed (default: `4242424242424242`) | `seed` |
| `cfg_coef` | classifier-free guidance coefficient | `cfg_coef` |
| `eos_threshold` | end-of-speech threshold | `eos_threshold` |
| `duration_delta` | speaking-rate offset; negative is faster | `conditions={"duration_delta": ...}` |

The first run downloads the default model (Phonon 31m, about 65 MB,
Apache-2.0) into the Hugging Face cache; later runs reuse it. Larger
Phonon 40m models are gated: accept their terms on the Hugging Face
model page, then set `HF_TOKEN` or run `hf auth login`. The 40m models
are released under CC BY-NC 4.0. `--backend-bin` and `$T2S_BACKEND_BIN`
do not apply to this backend, and bad `-O` keys or values are rejected
at startup with exit code 2. Free-threaded CPython builds cannot load
the prebuilt `ptts` wheels.

## Development checks

The project is formatted, linted, and type-checked before every commit:

```sh
uv format --preview-features format-command
uv run ruff check
uv run mypy --strict
uv run pytest -q
```
