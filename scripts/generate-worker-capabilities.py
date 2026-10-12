#!/usr/bin/env python3
"""Copy the shared worker-codex grants into every shipped worker template.

Only the Capabilities JSON block is generated; harness config and prose remain
authored in each profile. Run with --check to report drift without writing.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = re.compile(
    r"(^## Capabilities[ \t]*\n(?:(?!^## ).)*?^```json[ \t]*\n)(.*?)(^```)",
    re.MULTILINE | re.DOTALL,
)


def generate(defaults: Path, *, check: bool = False) -> list[Path]:
    source = defaults / "worker-codex" / "profile.md"
    match = CAPABILITIES.search(source.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{source}: missing Capabilities JSON block")
    block = match.group(2)
    json.loads(block)  # Refuse malformed shared grants before writing any copies.
    changes = []
    for path in sorted(defaults.glob("worker-*/profile.md")):
        if path == source:
            continue
        text = path.read_text(encoding="utf-8")
        target = CAPABILITIES.search(text)
        if target is None:
            raise ValueError(f"{path}: missing Capabilities JSON block")
        updated = text[:target.start(2)] + block + text[target.end(2):]
        if updated != text:
            changes.append(path)
            if not check:
                path.write_text(updated, encoding="utf-8")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changes = generate(ROOT / "src" / "profiles" / "defaults", check=args.check)
    for path in changes:
        print(f"{'Stale' if args.check else 'Updated'}: {path.relative_to(ROOT)}")
    return 1 if args.check and changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
