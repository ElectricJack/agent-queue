#!/usr/bin/env python3
"""Refresh selected producer digests after reviewing their command audit evidence.

Return branches, contracts and review notes remain authored evidence. Review
those fields before refreshing a handler or explicitly selected helper digest.
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
    parser.add_argument("--helper", action="append",
                        help="Refresh only this existing helper under one --command.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report drift without writing (default).")
    mode.add_argument("--write", action="store_true", help="Write the selected reviewed digests.")
    args = parser.parse_args()
    if args.helper is not None:
        if len(args.command) != 1 or len(args.helper) != 1:
            parser.error("--helper requires exactly one helper and one --command")
        name = args.command[0]
        helper = args.helper[0]
        helpers = rows[name].get("helper_producers", {})
        if helper not in helpers:
            parser.error(f"{name} has no audited helper {helper!r}")
        targets = [(f"{name}.{helper}", helper, helpers, helper)]
    else:
        targets = [(name, "_cmd_" + name, rows[name], "source_digest")
                   for name in dict.fromkeys(args.command)]
    changed = []
    for name, producer, evidence, key in targets:
        source = inspect.getsource(getattr(CommandHandler, producer))
        tree = ast.parse(textwrap.dedent(source))
        digest = "sha256:" + hashlib.sha256(
            ast.dump(tree, include_attributes=False).encode()
        ).hexdigest()
        if evidence[key] != digest:
            changed.append(name)
            evidence[key] = digest
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
