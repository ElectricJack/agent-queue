#!/usr/bin/env python3
"""Merge a complete set of default CI shard artifacts into pytest-split's map."""

import argparse
import json
import math
from pathlib import Path


def merge_durations(directory: Path, shards: int) -> dict[str, float]:
    """Validate every shard before returning their disjoint union."""
    if shards < 1:
        raise ValueError("shards must be positive")
    files = sorted(directory.rglob("default-*.json"))
    expected = {f"default-{group}.json" for group in range(1, shards + 1)}
    if len(files) != shards or {path.name for path in files} != expected:
        raise ValueError(f"expected exactly one artifact for each of {sorted(expected)}")

    merged = {}
    for path in files:
        durations = json.loads(path.read_text())
        if not isinstance(durations, dict) or not durations:
            raise ValueError(f"{path}: expected a nonempty duration map")
        for nodeid, duration in durations.items():
            if not nodeid.startswith("tests/") or "::" not in nodeid:
                raise ValueError(f"{path}: invalid test ID {nodeid!r}")
            if (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration < 0
            ):
                raise ValueError(f"{path}: invalid duration for {nodeid}")
            if nodeid in merged:
                raise ValueError(f"{path}: duplicate test ID {nodeid}")
            merged[nodeid] = duration
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="downloaded shard artifact directory")
    parser.add_argument("--shards", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path(".test_durations"))
    args = parser.parse_args()
    try:
        durations = merge_durations(args.directory, args.shards)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(durations, sort_keys=True, indent=4) + "\n")
    print(f"Stored {len(durations)} test timings in {args.output}")


if __name__ == "__main__":
    main()
