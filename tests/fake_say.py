#!/usr/bin/env python3
"""Offline stand-in for say(1): writes a tiny valid WAV instead of speaking.

Mimics the command-line surface t2s uses: `... -o OUT.wav` with the text on
stdin.  Environment variables:

    FAKE_SAY_FAIL_TEXT  if the input contains this substring, exit 1 with a
                        fake synthesis error on stderr
    FAKE_SAY_LOG        append "OUT<TAB>text-prefix" per render so tests can
                        map cache files back to paragraph text
"""

import os
import sys
import wave


def main() -> int:
    data = sys.stdin.read()
    fail = os.environ.get("FAKE_SAY_FAIL_TEXT")
    if fail and fail in data:
        sys.stderr.write("fake synthesis error: voice pack missing\n")
        return 1

    out = None
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "-o" and i + 1 < len(args):
            out = args[i + 1]
    if out is None:
        sys.stderr.write("fake say: no -o argument\n")
        return 2

    log = os.environ.get("FAKE_SAY_LOG")
    if log:
        # t2s renders to OUT.part then renames; log the final path so tests
        # can map play-log entries back to paragraph text.
        key_path = out[:-5] if out.endswith(".part") else out
        with open(log, "a") as f:
            f.write(f"{key_path}\t{' '.join(data.split())[:60]}\n")

    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)  # 0.1 s of silence
    return 0


if __name__ == "__main__":
    sys.exit(main())
