"""Assemble authored bookkeeping shards without inferring policy from live code.

Source modules own their audit/ownership rows. Policy sections and historical
measurements remain explicit reviewed inputs; assembly never measures a baseline
or refreshes a digest. Conflicting edits to the same evidence still need review.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OWNERSHIP_NAMES = ("integration_ownership.json", "integration_ownership_report.json")
SOURCE = Path("tests/fixtures/generated_sources")
AUDIT_MODULES = Path("tests/fixtures/contracts/command_audit_modules")
AUDIT_OUTPUT = Path("tests/fixtures/contracts/generated-command-audit.json")
DESCRIPTOR_OUTPUT = Path("tests/test_command_descriptors.py")


def render(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def merge(target: dict, incoming: dict, source: Path) -> None:
    """Combine disjoint evidence only, refusing ambiguous duplicate ownership."""
    for key, value in incoming.items():
        if key not in target:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target[key], dict):
            merge(target[key], value, source)
        elif isinstance(value, list) and isinstance(target[key], list):
            if any(item in target[key] for item in value):
                raise ValueError(f"{source}: duplicate evidence in {key}")
            target[key].extend(value)
        else:
            raise ValueError(f"{source}: duplicate evidence for {key}")


def load_shards(directory: Path) -> dict:
    paths = sorted(directory.rglob("*.json"))
    if not paths:
        raise ValueError(f"missing bookkeeping inputs: {directory}")
    result = {}
    for path in paths:
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError(f"{path}: expected an evidence object")
        merge(result, payload, path)
    return result


def audit_module(row: dict) -> Path:
    producer = row["producer"].split(":", 1)[0]
    path = Path(producer)
    if path.is_absolute() or ".." in path.parts or not producer.startswith("src/"):
        raise ValueError(f"invalid audit producer: {producer}")
    return Path(producer + ".json")


def bookkeeping_outputs(root: Path) -> dict[Path, bytes]:
    result = {}
    for name in OWNERSHIP_NAMES:
        payload = load_shards(root / SOURCE / "ownership/modules" / Path(name).stem)
        for key in ("owned_fingerprints", "owned_file_fingerprints"):
            if key in payload:
                payload[key].sort()
        # Keep the former canonical paths as generated compatibility projections
        # for branches and operator tooling that still read them.
        data = render(payload)
        result[SOURCE / "ownership" / name] = data
        result[Path("tests") / name] = data
    audit = load_shards(root / AUDIT_MODULES)
    seen = set()
    for path in sorted((root / AUDIT_MODULES).rglob("*.json")):
        for name, row in json.loads(path.read_text()).items():
            if name in seen:
                raise ValueError(f"{path}: duplicate audit command {name}")
            seen.add(name)
            if path.relative_to(root / AUDIT_MODULES) != audit_module(row):
                raise ValueError(f"{path}: audit row belongs to a different producer module")
    result[AUDIT_OUTPUT] = render(audit)
    fragments = sorted((root / SOURCE / "command_descriptors").glob("*.py.inc"))
    if not fragments:
        raise ValueError("missing command descriptor source fragments")
    result[DESCRIPTOR_OUTPUT] = b"".join(path.read_bytes() for path in fragments)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        outputs = bookkeeping_outputs(args.root)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Bookkeeping input refused: {exc}\n")
    stale = []
    for path, payload in outputs.items():
        destination = args.root / path
        if destination.exists() and destination.read_bytes() == payload:
            continue
        if args.check:
            stale.append(path)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
    for path in stale:
        print(f"Bookkeeping artifact drift: {path}")
    return int(bool(stale))


if __name__ == "__main__":
    raise SystemExit(main())
