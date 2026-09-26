"""Bounded, daemon-side observations of a configured default branch.

Only the caller inside the daemon supplies a checkout/ref. Public preview
arguments never select a path or git expression. All range operands are frozen
object ids; these reads never fetch, checkout, or update the repository.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from src.git.manager import GitManager

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
MAX_COMMITS = 200
MAX_FILES = 200
MAX_PROOFS = 200


async def read_git_evidence(
    git: GitManager,
    *,
    checkout: str,
    default_branch: str,
    since: float,
    until: float,
    now: float,
    previous_head: str | None = None,
    proof_commits: tuple[str, ...] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "head": None,
        "previous_head": previous_head,
        "ref": None,
        "commits": [],
        "files": [],
        "ancestry": {},
        "gaps": [],
        "warnings": [],
    }
    deadline = time.monotonic() + 30

    async def run(args: list[str]):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("git evidence read budget exhausted")
        return await git.arun_git_result(args, cwd=checkout, timeout=min(15, remaining))

    async def resolve(ref: str) -> str | None:
        observed = await run(["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"])
        sha = observed.stdout.strip()
        return sha if observed.returncode == 0 and _SHA.fullmatch(sha) else None

    try:
        valid = await run(["check-ref-format", f"refs/heads/{default_branch}"])
        if valid.returncode:
            result["gaps"].append("invalid_default_branch")
            return result
        for ref in (f"refs/remotes/origin/{default_branch}", f"refs/heads/{default_branch}"):
            head = await resolve(ref)
            if head:
                result.update(head=head, ref=ref)
                break
        else:
            result["gaps"].append("default_head_unavailable")
            return result

        if result["ref"].startswith("refs/remotes/"):
            path = await run(["rev-parse", "--git-path", "FETCH_HEAD"])
            fetch_file = Path(path.stdout.strip())
            if not fetch_file.is_absolute():
                fetch_file = Path(checkout) / fetch_file
            try:
                fetched_at = (await asyncio.to_thread(fetch_file.stat)).st_mtime
            except OSError:
                fetched_at = None
            result["fetched_at"] = fetched_at
            # A local observation cannot establish the current network head.
            result["warnings"].append("remote_head_not_network_verified")
            if fetched_at is None or now - fetched_at > 3600:
                result["gaps"].append("stale_remote_tracking_head")

        operands = [head]
        if previous_head is not None:
            old = await resolve(previous_head) if _SHA.fullmatch(previous_head) else None
            ancestor = await run(["merge-base", "--is-ancestor", old, head]) if old else None
            if ancestor is None or ancestor.returncode:
                result["gaps"].append("history_discontinuity")
                return result
            operands = [f"{old}..{head}"]
            result["warnings"].append("range_observed_at_build_not_commit_landing_times")
        else:
            result["warnings"].append("initial_history_uses_commit_dates_not_landing_times")
            result["gaps"].append("initial_history_no_previous_head")

        date_args = [] if previous_head else [f"--since-as-filter=@{since}", f"--until=@{until}"]
        log = await run(
            [
                "log",
                f"--max-count={MAX_COMMITS + 1}",
                "--format=%H%x00%ct%x00%<(300,trunc)%s",
                *date_args,
                *operands,
                "--",
            ]
        )
        if log.returncode:
            result["gaps"].append("history_discontinuity")
            return result
        lines = log.stdout.splitlines()
        if len(lines) > MAX_COMMITS:
            result["gaps"].append("commit_limit")
        for line in lines[:MAX_COMMITS]:
            sha, at, subject = line.split("\x00", 2)
            if previous_head or since <= float(at) < until:
                result["commits"].append(
                    {"sha": sha, "at": float(at), "subject": subject.rstrip()[:300]}
                )

        # First builds have no proven range base: expose only per-commit names.
        # Git itself bounds diffstat output: do not buffer an unlimited numstat.
        stat = [f"--stat=100,70,{MAX_FILES}", "--no-renames"]
        if previous_head:
            diff = await run(["diff", *stat, previous_head, head, "--"])
        elif result["commits"]:
            diff = await run(
                [
                    "show",
                    "--format=",
                    *stat,
                    *[commit["sha"] for commit in result["commits"]],
                    "--",
                ]
            )
        else:
            diff = None
        if diff is not None:
            if diff.returncode:
                result["gaps"].append("diff_unavailable")
            else:
                output = diff.stdout.strip()
                if len(output) > 8000 or "..." in output:
                    result["gaps"].append("file_limit")
                result["diffstat"] = output[:8000]

        if len(proof_commits) > MAX_PROOFS:
            result["gaps"].append("proof_limit")
        for commit in sorted(set(proof_commits))[:MAX_PROOFS]:
            if not _SHA.fullmatch(commit):
                result["ancestry"][commit] = "unknown"
                result["gaps"].append("invalid_proof_commit")
                continue
            resolved = await resolve(commit)
            if not resolved:
                result["ancestry"][commit] = "unknown"
                result["gaps"].append("proof_object_missing")
                continue
            proof = await run(["merge-base", "--is-ancestor", resolved, head])
            result["ancestry"][commit] = (
                "landed"
                if proof.returncode == 0
                else "pending"
                if proof.returncode == 1
                else "unknown"
            )
    except Exception:
        result["gaps"].append("git_read_failed")
    result["gaps"] = sorted(set(result["gaps"]))
    return result
