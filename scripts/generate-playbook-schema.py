#!/usr/bin/env python3
"""Generate ``src/playbook_v2_schema.json`` from the Playbook V2 Pydantic model.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md`` §8.

    python scripts/generate-playbook-schema.py            # write src/playbook_v2_schema.json
    python scripts/generate-playbook-schema.py --check    # exit 1 on drift, print a unified diff

``src/playbooks/definition.py`` is the single schema authority: the published
JSON Schema is a projection of the model that also loads and validates the
artifact, so the two cannot become independent interpretations (§1.5).

Determinism comes from three choices, all of them load-bearing:

* ``sort_keys=True`` — Pydantic's ``$defs`` insertion order is not stable across
  refactors, so the raw dict order is not a safe on-disk contract;
* ``mode="serialization"`` — the schema must describe what is *written*, which is
  what ``canonical_bytes`` hashes;
* a fixed ``ref_template``.

The script imports only ``src.playbooks.definition``: no daemon, no database and
no config are needed to run it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.playbooks.definition import PlaybookDefinition

DEFAULT_OUTPUT = REPO_ROOT / "src" / "playbook_v2_schema.json"


def render_schema() -> str:
    """Return the canonical JSON Schema text for :class:`PlaybookDefinition`."""
    schema = PlaybookDefinition.model_json_schema(
        by_alias=True, ref_template="#/$defs/{model}", mode="serialization"
    )
    return json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 and print a unified diff if the file is stale",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"schema file to write or check (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args(argv)

    generated = render_schema()
    current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""

    if args.check:
        if current == generated:
            return 0
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            generated.splitlines(keepends=True),
            fromfile=f"{args.output} (checked in)",
            tofile=f"{args.output} (generated)",
        )
        sys.stdout.writelines(diff)
        print(
            f"\n{args.output} is stale — run: python scripts/generate-playbook-schema.py",
            file=sys.stderr,
        )
        return 1

    if current != generated:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(generated, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(f"{args.output} is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
