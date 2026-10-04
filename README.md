# t2s

**NOTE:** This project is created using an LLM (mostly GLM 5.3 Flash)

**t2s** reads documents aloud on macOS, paragraph by paragraph, using the
built-in `say(1)` synthesizer. It shows each paragraph in the terminal as it
is spoken, synthesizes ahead of playback so paragraphs flow into each other
gaplessly, and gives you pause/skip control with single keystrokes.

## Requirements

- macOS (for `say`, `afplay`, and CoreAudio)
- Python 3.10+ (installed for you by `uv`)

## Install

```sh
uv tool install git+https://github.com/cjbayliss/t2s
```

To update later:

```sh
uv tool upgrade t2s
```

## Usage

```sh
t2s [options] [file]
```

Read *file* aloud. If *file* is omitted or is `-`, t2s reads standard input,
so you can pipe anything in:

```sh
t2s article.txt
t2s book.txt --start 42          # resume from paragraph 42
t2s -v Samantha -r 180 doc.txt   # choose a voice and speaking rate
pandoc --to=plain paper.md | t2s # read the plain-text output of a pipeline
curl -s https://example.com/post | t2s --split-long 600
t2s --gap 300 notes.txt          # 300 ms of silence between paragraphs
```

### Keys

While reading, t2s prints a dim header (`- 3/57 -`) followed by the
current paragraph, wrapped to the display width. Keys are read from the
terminal (from `/dev/tty` if stdin is piped):

| Key       | Action                                          |
| --------- | ----------------------------------------------- |
| `space`   | pause; press again to replay the paragraph      |
| `n`       | skip to the next paragraph                      |
| `p`       | go back to the previous paragraph               |
| `q` / ^C  | quit                                            |

Without a terminal for keys, t2s runs straight through, skipping paragraphs
it cannot render or play.

### Options

```
file                   text file to read ('-' or omitted: standard input)
-v, --voice VOICE      voice name passed to say
-r, --rate WPM         speech rate in words per minute
--width COLS           display wrap width (default: 72)
--start N              paragraph number to start from (1-based)
--split-long CHARS     also split paragraphs longer than CHARS at sentence
                       boundaries
--ahead N              paragraphs to synthesize ahead of playback
                       (default: 3; raise for slow premium voices)
--cache-dir PATH       audio cache directory (default: ~/Library/Caches/t2s)
--cache-limit-mb MB    prune the cache when larger than this
                       (default: 256; 0 = unlimited)
--gap MS               silence between paragraphs in milliseconds (default: 0)
--data-format FMT      synthesis format for say, e.g. LEI16@48000
                       (default: LEI16 at the output device's native rate)
--say-bin PATH         say binary to run (default: say, or $T2S_SAY_BIN)
--play-bin PATH        audio player binary (default: afplay, or $T2S_PLAY_BIN)
--player ENGINE        playback engine: miniaudio (gapless in-process
                       streaming), afplay (external process fallback),
                       test (headless, for t2s's own tests), or auto
                       (default: miniaudio, falls back to afplay)
--version              show the version and exit
```

### Environment

| Variable       | Purpose                             |
| -------------- | ----------------------------------- |
| `T2S_SAY_BIN`  | default for `--say-bin`             |
| `T2S_PLAY_BIN` | default for `--play-bin`            |
