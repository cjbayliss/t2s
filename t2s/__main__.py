"""python -m t2s — same entry point as the installed script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
