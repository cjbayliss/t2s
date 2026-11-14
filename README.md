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

For the Phonon backend, also install the `phonon` extra, which pulls in
the `ptts` Python bindings:

```sh
uv tool install git+https://github.com/cjbayliss/t2s --with ptts
```

## Requirements

- Python 3.15 or later
- [`uv`](https://docs.astral.sh/uv/) for installation and development
- The `miniaudio` Python package for gapless playback (installed
  automatically; without it, t2s falls back to `afplay`)
- Optional: the `ptts` Python package for the optional Phonon backend
  (installed with the `phonon` extra; it ships prebuilt wheels for
  supported platforms and needs no PyTorch)
- Optional: `ffmpeg` on `PATH` for Phonon's `-O sample_rate=...`
  upsampling
- Optional (if using the say backend): macOS

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
