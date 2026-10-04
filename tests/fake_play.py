import os
import sys
import time


def main() -> int:
    args = sys.argv[1:]
    path = args[-1] if args else "?"

    log_path = os.environ.get("FAKE_PLAY_LOG")
    count = 0
    if log_path:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(path + "\n")
        with open(log_path, encoding="utf-8") as log_file:
            count = sum(1 for line in log_file if line.strip())

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
