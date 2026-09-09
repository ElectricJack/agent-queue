#!/usr/bin/env python3
"""Generate or verify the maintained ``aq`` CLI command inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "reference" / "cli-command-inventory.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _render() -> str:
    from src.cli.app import cli
    from src.cli.inventory import build_cli_inventory

    return json.dumps(build_cli_inventory(cli), indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the artifact is stale.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rendered = _render()
    output = args.output.resolve()
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != rendered:
            print(f"CLI inventory is stale: run {Path(__file__).name}")
            return 1
        inventory = json.loads(rendered)
        print(f"CLI inventory current: {inventory['counts']['leaf_commands']} leaf commands")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    inventory = json.loads(rendered)
    print(f"Wrote {output}: {inventory['counts']['leaf_commands']} leaf commands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
