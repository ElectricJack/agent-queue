"""Validate the npm projection and both dashboard obligations as one stable suite."""

import argparse
import json
from pathlib import Path

from tools.bazel.import_npm import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--typecheck", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    args = parser.parse_args()
    check(Path.cwd())
    for kind in ("typecheck", "build"):
        result = json.loads((getattr(args, kind) / "result.json").read_text())
        if result != {"command": kind, "success": True}:
            raise ValueError(f"missing dashboard obligation: {kind}")
    if not (args.build / "dist/index.html").is_file():
        raise ValueError("missing dashboard build output")
    print("Dashboard typecheck/build and npm/pnpm graph equivalence: exact")


if __name__ == "__main__":
    main()
