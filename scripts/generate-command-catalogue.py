#!/usr/bin/env python3
"""Generate or verify packaged command definitions for the CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from src.tools.command_catalogue import CATALOGUE_PATH, render_command_catalogue

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the artifact is stale.")
    args = parser.parse_args()
    rendered = render_command_catalogue()
    if args.check:
        if not CATALOGUE_PATH.exists() or CATALOGUE_PATH.read_text(encoding="utf-8") != rendered:
            print("Command catalogue is stale: run scripts/generate-command-catalogue.py")
            return 1
        print("Command catalogue current")
    else:
        CATALOGUE_PATH.write_text(rendered, encoding="utf-8")
        print(f"Wrote {CATALOGUE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
