"""Git low-level generated merge driver with a reviewed-evidence fence."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

# Git invokes this installed file directly, including from unrelated repositories.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.git.manager import GitError, GitManager
from src.integration.bookkeeping_guard import (
    LEGACY_SOURCES, LegacyBookkeepingConflict, check_merge, check_worktree,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-worktree", type=Path)
    parser.add_argument("--base-ref")
    parser.add_argument("--current-ref")
    parser.add_argument("--other-ref")
    parser.add_argument("blobs", nargs="*")
    args = parser.parse_args()
    try:
        if args.check_worktree:
            await check_worktree(args.check_worktree)
            return 0
        base, current, other, path = args.blobs
        if path in LEGACY_SOURCES:
            changed = any(Path(side).read_bytes() != Path(base).read_bytes()
                          for side in (current, other))
            if changed:
                if not all((args.base_ref, args.current_ref, args.other_ref)):
                    raise LegacyBookkeepingConflict([path])
                git = GitManager()
                # Bind the explicit context to the actual blobs Git supplied.
                for ref, blob in zip((args.base_ref, args.current_ref, args.other_ref),
                                     (base, current, other), strict=True):
                    contents = await git.arun_git_result(
                        ["show", f"{ref}:{path}"], cwd=str(Path.cwd()),
                    )
                    if contents.returncode or contents.stdout.encode() != Path(blob).read_bytes():
                        raise LegacyBookkeepingConflict([path])
                await check_merge(git, Path.cwd(), args.base_ref,
                                  args.current_ref, args.other_ref)
        process = await asyncio.create_subprocess_exec(
            "git", "merge-file", "--quiet", "--ours", current, base, other,
        )
        return await process.wait()
    except (LegacyBookkeepingConflict, GitError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
