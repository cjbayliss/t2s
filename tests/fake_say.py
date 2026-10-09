import os
import sys
import wave


def main() -> int:
    text = sys.stdin.read()
    fail_text = os.environ.get("FAKE_SAY_FAIL_TEXT")
    if fail_text and fail_text in text:
        sys.stderr.write("fake synthesis error: voice pack missing\n")
        return 1

    output_path = None
    arguments = sys.argv[1:]
    for position, argument in enumerate(arguments):
        if argument == "-o" and position + 1 < len(arguments):
            output_path = arguments[position + 1]
    if output_path is None:
        sys.stderr.write("fake say: no -o argument\n")
        return 2

    log_path = os.environ.get("FAKE_SAY_LOG")
    if log_path:
        logged_path = output_path.removesuffix(".part")
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"{logged_path}\t{' '.join(text.split())[:60]}\n")

    corrupt_text = os.environ.get("FAKE_SAY_CORRUPT_TEXT")
    if corrupt_text and corrupt_text in text:
        with open(output_path, "wb") as corrupt_file:
            corrupt_file.write(b"plainly not riff data")
        return 0

    with wave.open(output_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 800)
    return 0


if __name__ == "__main__":
    sys.exit(main())
