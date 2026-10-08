#!/usr/bin/env python3
"""One-time, exact-tip cleanup for branches whose content reached dev or main.

The default is a read-only dry run. Applying a plan requires the operator's
configured GitHub credentials and database: live tasks, branch owners and
unsettled integrations are held even when their current remote tip is already
present on a target branch. Unique preserved work is always listed and kept.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from src.git.manager import GitError, GitManager, RemoteRefState, is_valid_git_oid
from src.integration.delivery_branches import _merges_are_clean, live_branch_references, remote_heads
from src.projects.github import GitHubError, parse_github_repository


REPOSITORY = "ElectricJack/agent-queue"
PROTECTED = frozenset({"main", "dev", "staging"})
COUNT_PREFIXES = (
    "aq/epic/",
    "aq/preserved/",
    "aq/integration-repairs/",
    "aq/parent/",
    "aq/integration/",
    "aq/development/",
    "aq-provenance/",
)
DEFAULT_CHECKOUT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP = (
    DEFAULT_CHECKOUT / "vault/projects/agent-queue/notes/origin-ref-cleanup-2026-10-08.tsv"
)


def _counts(heads: dict[str, str]) -> dict[str, int]:
    return {prefix: sum(name.startswith(prefix) for name in heads) for prefix in COUNT_PREFIXES}


async def _run(git: GitManager, store: str, *args: str) -> str:
    result = await git.arun_git_result(list(args), cwd=store)
    if result.returncode:
        raise GitError(result.stderr or f"git {args[0]} failed")
    return result.stdout.strip()


async def _patch_equivalent(git: GitManager, store: str, head: str, target: str) -> bool:
    """Prove every branch commit has a patch-id twin and every merge is clean."""
    common_base = await git.arun_git_result(["merge-base", target, head], cwd=store)
    if common_base.returncode:
        return False
    merges = await _run(git, store, "rev-list", "--merges", f"{target}..{head}")
    if merges and not await _merges_are_clean(_run_adapter(git, store), store, target, head):
        return False
    result = await git.arun_git_result(["cherry", target, head], cwd=store)
    if result.returncode:
        return False
    rows = [line for line in result.stdout.splitlines() if line]
    return bool(rows) and all(row.startswith("-") for row in rows)


def _run_adapter(git: GitManager, store: str):
    async def run(_store: str, *args: str) -> str:
        return await _run(git, store, *args)

    return run


async def _plan(git: GitManager, store: str) -> tuple[dict[str, dict[str, str]], list[dict[str, str]], dict[str, str]]:
    heads = await remote_heads(_run_adapter(git, store), store)
    targets = {name: heads[name] for name in ("dev", "main") if name in heads}
    if not targets:
        raise GitError("origin has neither dev nor main; refusing to classify refs")

    deletable: dict[str, dict[str, str]] = {}
    preserved: list[dict[str, str]] = []
    for branch, head in sorted(heads.items()):
        if branch in PROTECTED or branch.startswith("aq-provenance/"):
            continue
        proof = None
        for target_name, target_sha in targets.items():
            if not is_valid_git_oid(head) or not is_valid_git_oid(target_sha):
                continue
            ancestor = await git.ais_ancestor(store, head, target_sha, strict=True)
            if ancestor is True:
                proof = f"ancestor:{target_name}"
                break
            if ancestor is None:
                continue
            if await _patch_equivalent(git, store, head, target_sha):
                proof = f"patch-equivalent:{target_name}"
                break
        if proof:
            deletable[branch] = {"head": head, "reason": proof}
        elif branch.startswith("aq/preserved/"):
            preserved.append({"branch": branch, "head": head})
    return deletable, preserved, heads


async def _load_operator_holds():
    from sqlalchemy import text

    from src.config import load_config
    from src.database.engine import create_postgres_engine

    config = load_config(os.path.expanduser("~/.agent-queue/config.yaml"))
    engine = create_postgres_engine(config.database.url, 1, 2)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                holds = await live_branch_references(conn)
        return config, holds
    finally:
        await engine.dispose()


def _append_backup(path: Path, branch: str, head: str, proof: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if new_file:
            handle.write("ref\tsha\tproof\tprepared_at_utc\n")
        handle.write(
            f"refs/heads/{branch}\t{head}\t{proof}\t"
            f"{datetime.now(UTC).isoformat()}\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def _print_report(label: str, heads: dict[str, str]) -> None:
    print(f"{label} branch heads: {len(heads)}")
    for prefix, count in _counts(heads).items():
        print(f"  {prefix}: {count}")


async def _main(args: argparse.Namespace) -> int:
    checkout = args.checkout.resolve()
    git = GitManager()
    remote_url = await _run(git, str(checkout), "config", "--get", "remote.origin.url")
    try:
        full_name = parse_github_repository(remote_url).full_name
    except GitHubError as exc:
        raise GitError("origin is not a recognizable GitHub repository") from exc
    if full_name.casefold() != REPOSITORY.casefold():
        raise GitError(f"origin is {full_name}; expected {REPOSITORY}")

    await git.afetch_origin(str(checkout), repository_url=remote_url, all_heads=True)
    plan, preserved, before = await _plan(git, str(checkout))
    _print_report("Before", before)
    print(f"Git-proven deletion candidates: {len(plan)}")
    counts = Counter(value["reason"] for value in plan.values())
    for reason, count in sorted(counts.items()):
        print(f"  {reason}: {count}")
    print(f"Unique preserved refs kept: {len(preserved)}")
    for row in preserved:
        print(f"  {row['branch']} {row['head']}")

    if not args.apply:
        print("Dry run only; origin was not changed.")
        _print_report("After (dry run)", before)
        return 0

    from src.database.migration_guard import WORKER, current_scope

    if current_scope() == WORKER or os.environ.get("AQ_DB_SCOPE") == "worker":
        raise GitError("apply is operator-only; this worker must not read the operator database")

    config, holds = await _load_operator_holds()
    # Re-read target and candidate heads after the database snapshot. The
    # branch delete itself remains leased to the exact observed candidate tip.
    await git.afetch_origin(str(checkout), repository_url=remote_url, all_heads=True)
    plan, preserved, before = await _plan(git, str(checkout))
    _print_report("Rechecked before apply", before)
    blocked = {branch: reason for branch, reason in holds.items() if branch in plan}
    for branch in blocked:
        plan.pop(branch, None)
    print(f"Held by live task, owner or integration state: {len(blocked)}")

    from src.git.github import GitHubAccess

    git = GitManager(GitHubAccess.from_config(config.integration.github_app))
    binding = await git._abind_git_repository(remote_url)
    deleted = []
    for branch, entry in sorted(plan.items()):
        ref = "refs/heads/" + branch
        current = await git.als_remote_ref(str(checkout), branch)
        if current.state is RemoteRefState.ERROR:
            print(f"  failed {branch}: remote state unknown")
            continue
        if current.state is RemoteRefState.ABSENT:
            continue
        if current.oid != entry["head"]:
            print(f"  moved {branch}: expected head no longer matches")
            continue
        _append_backup(args.backup, branch, entry["head"], entry["reason"])
        try:
            if binding is None:
                await git.adelete_remote_ref_exact(str(checkout), ref, entry["head"])
            else:
                await git.adelete_repository_ref(
                    str(checkout), repository=binding, branch=branch,
                    expected_old_oid=entry["head"],
                )
        except GitError:
            pass
        after = await git.als_remote_ref(str(checkout), branch)
        if after.state is RemoteRefState.ABSENT:
            deleted.append(branch)
            print(f"  deleted {branch} {entry['head']}")
        elif after.state is RemoteRefState.PRESENT and after.oid != entry["head"]:
            print(f"  moved {branch}: new remote head retained")
        else:
            print(f"  failed {branch}: deletion could not be confirmed")

    await git.afetch_origin(str(checkout), repository_url=remote_url, all_heads=True)
    after_heads = await remote_heads(_run_adapter(git, str(checkout)), str(checkout))
    print(f"Deleted branches: {len(deleted)}")
    print(f"Backup: {args.backup}")
    _print_report("After", after_heads)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=DEFAULT_CHECKOUT)
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument("--apply", action="store_true", help="delete Git-proven, unheld refs")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_main(args)))
    except (GitError, OSError, ValueError) as exc:
        print(f"cleanup refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
