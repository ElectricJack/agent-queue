#!/usr/bin/env python3
"""Project approved recordings and historical ownership inputs into generated copies.

Canonical module shards and playbook recordings are authored and merged
normally; the old aggregate ownership paths are compatibility projections.
This command never compiles policy, invents an approval or measures a new
historical baseline. Approved definition timestamps and manifest bindings remain
exact; a changed definition needs its matching approved manifest input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INPUTS = Path("tests/fixtures/generated_sources")
BUNDLE_FILES = ("artifact.json", "artifact.sha256", "source.md", "manifest.md", "diagnostics.json")


def outputs(root: Path) -> dict[Path, bytes]:
    from scripts.bookkeeping_sources import bookkeeping_outputs
    from src.playbooks.definition import source_digest

    inputs = root / INPUTS
    result = bookkeeping_outputs(root)
    index = json.loads((inputs / "playbooks/index.json").read_text())
    for playbook_id, destinations in index.items():
        source = inputs / "playbooks" / playbook_id
        definition = json.loads((source / "definition.json").read_text())
        artifact = json.dumps(
            definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
        manifest = (source / "manifest.md").read_bytes()
        binding = re.search(rb"^artifact_sha256: (\S+)$", manifest, re.M)
        if binding is None or binding[1].decode() != digest:
            raise ValueError(f"{playbook_id}: definition differs from its approved manifest binding")
        files = {name: (source / name).read_bytes() for name in BUNDLE_FILES[2:]}
        source_binding = re.search(rb"^source_sha256: (\S+)$", manifest, re.M)
        if (
            source_digest(files["source.md"].decode()) != definition["source_hash"]
            or source_binding is None
            or source_binding[1].decode() != definition["source_hash"]
        ):
            raise ValueError(f"{playbook_id}: source differs from its approved definition binding")
        files.update({"artifact.json": artifact, "artifact.sha256": (digest + "\n").encode()})
        for destination in destinations:
            directory = Path(destination)
            if directory.is_absolute() or ".." in directory.parts:
                raise ValueError(f"invalid generated destination: {destination}")
            for name, payload in files.items():
                # The legacy operator-importable bundle has four files.
                if destination.startswith("docs/") and name == "diagnostics.json":
                    continue
                result[directory / name] = payload
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--list", action="store_true")
    args = parser.parse_args()
    try:
        artifacts = outputs(args.root)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Recorded artifact input refused: {exc}\n")
    stale = []
    for relative, payload in sorted(artifacts.items()):
        if args.list:
            print(relative.as_posix())
            continue
        path = args.root / relative
        if path.exists() and path.read_bytes() == payload:
            continue
        if args.check:
            stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
    if stale:
        for path in stale:
            print(f"Recorded artifact drift: {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
