#!/usr/bin/env python3
"""Check upgrades added between two immutable Git refs without importing them."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database.additive_migrations import check_range  # noqa: E402


async def git(repository: Path, *args: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repository),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise ValueError(stderr.decode().strip() or f"git {args[0]} failed")
    return stdout


async def resolve(repository: Path, ref: str) -> str:
    return (
        (await git(repository, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"))
        .decode()
        .strip()
    )


async def sources(repository: Path, oid: str) -> dict[str, str]:
    paths = (
        (await git(repository, "ls-tree", "-rz", "--name-only", oid, "--", "migrations/versions/"))
        .decode()
        .split("\0")
    )
    result = {}
    for path in paths:
        if path.endswith(".py") and Path(path).name != "__init__.py":
            result[path] = (await git(repository, "show", f"{oid}:{path}")).decode()
    return result


async def inspect_range(repository: Path, previous: str, current: str) -> dict:
    before, after = await resolve(repository, previous), await resolve(repository, current)
    await git(repository, "merge-base", "--is-ancestor", before, after)
    findings = check_range(await sources(repository, before), await sources(repository, after))
    return {
        "success": not findings,
        "previous": before,
        "current": after,
        "findings": [str(finding) for finding in findings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--from-ref", required=True)
    parser.add_argument("--to-ref", default="HEAD")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = asyncio.run(inspect_range(args.repository, args.from_ref, args.to_ref))
    except (ValueError, OSError, SyntaxError) as exc:
        report = {"success": False, "findings": [str(exc)]}
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print("Additive migrations: PASS" if report["success"] else "Additive migrations: FAIL")
        for finding in report["findings"]:
            print(finding)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
