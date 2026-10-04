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
        key_path = out[:-5] if out.endswith(".part") else out
        with open(log, "a") as f:
            f.write(f"{key_path}\t{' '.join(data.split())[:60]}\n")

    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return 0


if __name__ == "__main__":
    sys.exit(main())
