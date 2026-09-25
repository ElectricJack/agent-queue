#!/usr/bin/env python3
"""Generate or verify ``tests/selection_catalogue.json`` from ``tests/selection_areas.yaml``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from src.test_selection import catalogue as cat

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when the catalogue is stale.")
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root.")
    args = parser.parse_args()

    root = args.root.resolve()
    try:
        catalogue = cat.build_catalogue(root, cat.load_areas(root / cat.AREAS_PATH))
    except cat.CatalogueError as exc:
        for problem in exc.problems:
            print(problem, file=sys.stderr)
        print(f"Fix {cat.AREAS_PATH} so every runnable module matches an area.", file=sys.stderr)
        return 1
    rendered = cat.render_catalogue(catalogue)
    output = root / cat.CATALOGUE_PATH
    summary = f"{len(catalogue.modules)} modules in {len(catalogue.areas)} areas"
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != rendered:
            print(f"selection catalogue drifted; run `python scripts/{Path(__file__).name}`")
            return 1
        print(f"Selection catalogue current: {summary}")
        return 0
    output.write_text(rendered, encoding="utf-8")
    print(f"Wrote {output}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
