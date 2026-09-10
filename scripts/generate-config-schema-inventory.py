#!/usr/bin/env python3
"""Generate or verify the configuration schema reference from ``AppConfig``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "reference" / "configuration-schema.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def render() -> str:
    """Serialize the same stable schema exposed by ``aq system config schema``."""
    from src.config_editor import build_config_schema

    return json.dumps(build_config_schema(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the artifact is stale")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rendered = render()
    output = args.output.resolve()
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            print(f"configuration schema inventory is stale: run {Path(__file__).name}")
            return 1
        print("configuration schema inventory is current")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    try:
        display_path = output.relative_to(ROOT)
    except ValueError:
        display_path = output
    print(f"wrote {display_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
