"""Compatibility entry point; use `uv run synthocr` for the full CLI."""

import sys

from synthocr.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["generate"]))
