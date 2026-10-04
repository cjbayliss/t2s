# t2s

Read a document aloud with macOS [`say(1)`](https://ss64.com/mac/say.html),
**one paragraph at a time** — with gapless playback, pause/replay, a clean
72-column display, and no lost progress when audio devices connect or
disconnect.

Python 3.10+, macOS only. One dependency (`miniaudio`, self-contained wheel).

## Why

Driving `say` directly has four papercuts:

| Problem | t2s |
|---|---|
| `say -i` ignores the terminal width when piped | t2s prints each paragraph wrapped to a fixed width (default **72** columns) |
| `say` cannot pause | **Space** stops the current paragraph; **Space** again replays it from its beginning — instantly, from an in-memory buffer |
| `say` dies silently when an audio device appears/disappears | Synthesis is offline (it never touches the audio device); only playback does, so a device change can at worst interrupt the current paragraph. t2s reports it and waits — **Space** replays the cached file. Your place is never lost beyond the current paragraph |
| `say` cannot start mid-document | `--start N` begins at paragraph *N* (1-based, as shown in the header) |

## How it works

Each paragraph is synthesized offline to a small WAV file in a cache
directory (`say -o …`, pinned to 22 kHz 16-bit), a few paragraphs ahead of
playback (`--ahead`, default 3).

Playback is **in-process**: the default engine decodes the cached files
into memory and streams them through a single long-lived miniaudio device.
When a paragraph's samples run out, the stream chains straight into the
next preloaded paragraph's samples — **zero silence between paragraphs**
(or `--gap MS` milliseconds of it, your choice), no process spawning, no
device re-initialization. Pause/replay just stops and restarts the stream.

The cache (`~/Library/Caches/t2s`, one file per paragraph, keyed by text +
voice + rate + format) persists between runs: re-reading or resuming a
document skips synthesis entirely. It is pruned oldest-first when it grows
beyond `--cache-limit-mb` (default 256).

Playback uses the system's currently selected output device (System
Settings → Sound), so you can switch devices mid-book; if the device
vanishes mid-paragraph, t2s tells you and waits — press **Space** when the
new device is ready.

## Install

```sh
uv tool install .        # or: pipx install .
```

Or run in place: `./t2s.py chapter.txt` (or `python3 t2s.py chapter.txt`).
Without `miniaudio` installed, t2s falls back to playing each paragraph
with `afplay` (small gaps between paragraphs).

## Usage

```sh
t2s chapter.txt
cat chapter.txt | t2s --voice Fred -r 190
t2s chapter.txt --start 14
t2s chapter.txt --split-long 1200      # finer pause/replay granularity
```

### Keys

| Key | Action |
|---|---|
| `space` | stop / replay the current paragraph |
| `n` / `p` | next / previous paragraph |
| `q`, Ctrl-C | quit |

Keyboard input comes from the terminal, so `cat book.txt | t2s` stays
fully interactive. When no terminal is available (e.g. under a cron job),
t2s runs non-interactively: on a synthesis or playback failure it reports
the error and continues with the next paragraph.

### Options

```
file                  text file to read ('-' or omitted: standard input)
-v, --voice NAME      voice passed to say
-r, --rate WPM        speech rate in words per minute
--width COLS          display wrap width (default: 72)
--start N             paragraph number to start from (1-based)
--split-long CHARS    also split paragraphs longer than CHARS at sentence
                      boundaries
--ahead N             paragraphs to synthesize ahead of playback (default 3;
                      raise for slow "premium" voices)
--gap MS              silence between paragraphs in milliseconds
                      (default: 0 — seamless chaining)
--cache-dir PATH      audio cache directory (default: ~/Library/Caches/t2s)
--cache-limit-mb MB   prune cache when larger than this (default 256)
--player ENGINE       auto (default) | miniaudio | afplay | test
--say-bin PATH        say binary (default: say, or $T2S_SAY_BIN)
--play-bin PATH       afplay binary (default: afplay, or $T2S_PLAY_BIN)
--version
```

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e .[test]
.venv/bin/python -m pytest -q
```

Tests never touch audio (one test plays 0.2 s of silence through the real
device and is skipped if miniaudio is unavailable): `tests/fake_say.py`
writes tiny WAVs offline, the `--player test` engine is fully headless and
scriptable, and the interactive tests drive t2s through a pty pressing
real keys.
