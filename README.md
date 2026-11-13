# t2s

**NOTE:** This project is created using an LLM (mostly GLM 5.3 Flash)

Read documents aloud on macOS with `say(1)`, one paragraph at a time -
gapless in-process playback, pausable and skippable from the keyboard,
with a 72-column paragraph display that survives audio device changes.

## Installation

Install as a standalone tool from the repository:

```sh
uv tool install git+https://github.com/cjbayliss/t2s
```

or to run it from a local clone

```sh
uv run t2s
```

## Requirements

- macOS (uses the built-in `say` and `afplay` command-line tools)
- Python 3.15 or later
- [`uv`](https://docs.astral.sh/uv/) for installation and development
- The `miniaudio` Python package for gapless playback (installed
  automatically; without it, t2s falls back to `afplay`)

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
```

Each paragraph is synthesized ahead of playback into an audio cache and
streamed in-process, so paragraphs play without gaps. Paragraphs are
numbered from 1 in the displayed output, and `--start N` resumes from a
known position.

### Options

- `file` - text file to read; `-` or omitted reads standard input
- `-B, --backend NAME` - synthesis backend (default: `say`)
- `-O, --option KEY=VALUE` - option passed to the backend, repeatable;
  a single-letter key becomes a short flag (`-O r=350` -> `-r 350`),
  anything longer a long flag (`-O rate=350` -> `--rate 350`)
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
  own binary, or `$T2S_BACKEND_BIN`)
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

## Development checks

The project is formatted, linted, and type-checked before every commit:

```sh
uv format --preview-features format-command
uv run ruff check
uv run mypy --strict
uv run pytest -q
```
