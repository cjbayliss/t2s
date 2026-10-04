import os
import sys
import time


def main() -> int:
    args = sys.argv[1:]
    path = args[-1] if args else "?"

    log = os.environ.get("FAKE_PLAY_LOG")
    count = 0
    if log:
        with open(log, "a") as f:
            f.write(path + "\n")
        with open(log) as f:
            count = sum(1 for line in f if line.strip())

    fail_at = os.environ.get("FAKE_PLAY_FAIL_AT")
    if fail_at and count == int(fail_at):
        sys.stderr.write("fake playback error: output device vanished\n")
        return 3

    delay = float(os.environ.get("FAKE_PLAY_DELAY", "0.05") or 0)
    if delay:
        time.sleep(delay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
