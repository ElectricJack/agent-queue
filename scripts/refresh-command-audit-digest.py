#!/usr/bin/env python3
"""Refresh selected producer digests after reviewing their command audit evidence.

Return branches, contracts, helper producers and review notes remain authored
evidence. Review those fields before refreshing a changed handler's digest.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
AUDIT = ROOT / "tests/fixtures/contracts/generated-command-audit.json"


def main() -> int:
    from src.commands.handler import CommandHandler

    rows = json.loads(AUDIT.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", action="append", required=True, choices=sorted(rows),
                        help="Reviewed handler to refresh; repeat for multiple handlers.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report drift without writing (default).")
    mode.add_argument("--write", action="store_true", help="Write the selected reviewed digests.")
    args = parser.parse_args()
    changed = []
    for name in dict.fromkeys(args.command):
        source = inspect.getsource(getattr(CommandHandler, "_cmd_" + name))
        tree = ast.parse(textwrap.dedent(source))
        digest = "sha256:" + hashlib.sha256(
            ast.dump(tree, include_attributes=False).encode()
        ).hexdigest()
        if rows[name]["source_digest"] != digest:
            changed.append(name)
            rows[name]["source_digest"] = digest
    if changed:
        if not args.write:
            print("Command audit digest drift: " + ", ".join(changed))
            return 1
        AUDIT.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print("Refreshed command audit digests: " + ", ".join(changed))
    else:
        print("Selected command audit digests are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
