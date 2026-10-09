"""Conservative daily branch, worktree-registration and stash maintenance.

The sweep only deletes branch tips whose content is reachable from (or cleanly
patch-equivalent to) ``dev``, ``main`` or the repository default branch. It
records every deleted local or remote ref before mutation and never drops a
stash. Live AQ references and branches attached to any worktree are held.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.git.manager import GitError, GitManager, RemoteRefState, is_valid_git_oid
from src.integration.delivery_branches import (
    PROTECTED_BRANCHES, _merges_are_clean, branch_of, deletable, remote_heads,
)

PROTECTED_NAMES = frozenset({"main", "dev", "staging", "gh-pages"})


@dataclass
class BranchSweepReport:
    checkout: str
    local_before: int = 0
    local_after: int = 0
    remote_before: int = 0
    remote_after: int = 0
    local_deleted: list[tuple[str, str, str]] = field(default_factory=list)
    remote_deleted: list[tuple[str, str, str]] = field(default_factory=list)
    local_held: int = 0
    remote_held: int = 0
    unique_unmerged: list[tuple[str, str]] = field(default_factory=list)
    preserved_kept: list[tuple[str, str]] = field(default_factory=list)
    stale_worktrees_pruned: int = 0
    stashes: list[str] = field(default_factory=list)


async def _run(git: GitManager, checkout: str, *args: str) -> str:
    result = await git.arun_git_result(list(args), cwd=checkout)
    if result.returncode:
        raise GitError(result.stderr or f"git {args[0]} failed")
    return result.stdout.strip()


def _run_adapter(git: GitManager):
    async def run(checkout: str, *args: str) -> str:
        return await _run(git, checkout, *args)

    return run


async def _proof(
    git: GitManager, checkout: str, head: str, targets: dict[str, str]
) -> str | None:
    """Return ancestry/patch-equivalence evidence, or None for unique work."""
    if not is_valid_git_oid(head):
        return None
    for target_name, target in targets.items():
        if not is_valid_git_oid(target):
            continue
        ancestor = await git.ais_ancestor(checkout, head, target, strict=True)
        if ancestor is True:
            return f"ancestor:{target_name}"
        if ancestor is None:
            continue
        common = await git.arun_git_result(["merge-base", target, head], cwd=checkout)
        if common.returncode:
            continue
        cherry = await git.arun_git_result(["cherry", target, head], cwd=checkout)
        if cherry.returncode:
            continue
        rows = [line for line in cherry.stdout.splitlines() if line.strip()]
        if not rows or not all(line.startswith("-") for line in rows):
            continue
        merges = await _run(git, checkout, "rev-list", "--merges", f"{target}..{head}")
        if merges and not await _merges_are_clean(_run_adapter(git), checkout, target, head):
            continue
        return f"patch-equivalent:{target_name}"
    return None


async def clean_landed_checkout(
    git: GitManager,
    checkout: str,
    *,
    repository_url: str,
    default_branch: str,
    ref: str,
    expected_sha: str,
    target_sha: str,
    managed_paths: set[str],
    busy_paths: set[str],
    protected: frozenset[str],
    fetched_checkouts: set[str],
) -> tuple[str, str | None]:
    """Retire one landed head in a common Git directory, retaining worktrees.

    The caller excludes new claims/writers while this runs. Attached heads may
    only be detached in clean idle AQ checkouts; other worktrees remain holds.
    Both the local head and origin tracking ref use expected-old deletion.
    Daily sweeping keeps its broader patch-equivalence policy; landing requires
    strict ancestry of the recorded tip, including after origin was deleted.
    """
    name = branch_of(ref)
    if (not ref.startswith("refs/heads/") or not name
            or not deletable(name, default_branch, protected=protected | PROTECTED_NAMES)
            or not is_valid_git_oid(expected_sha) or not is_valid_git_oid(target_sha)):
        return "conflict", "local cleanup identity is protected or invalid"
    tracking = f"refs/remotes/origin/{name}"
    exists = await git.aref_exists(checkout, ref)
    tracked = await git.aref_exists(checkout, tracking)
    if exists is None or tracked is None:
        return "retryable", "local cleanup ref state is unknown"
    if not exists and not tracked:
        return "complete", None
    common = await _run(git, checkout, "rev-parse", "--git-common-dir")
    common_path = str((Path(checkout) / common).resolve())
    if common_path not in fetched_checkouts:
        await git.afetch_origin(checkout, repository_url=repository_url, all_heads=True)
        fetched_checkouts.add(common_path)
    head = await git.arev_parse(checkout, ref) if exists else None
    if exists and head != expected_sha:
        return "conflict", f"local ref moved after delivery: {checkout}"
    result = await git.arun_git_result(
        ["--no-replace-objects", "merge-base", "--is-ancestor", expected_sha, target_sha],
        cwd=checkout,
    )
    ancestor = {0: True, 1: False}.get(result.returncode)
    if ancestor is not True:
        return ("retryable" if ancestor is None else "conflict"), "local tip is not proven landed"

    attached = [row for row in await git.aworktree_list(checkout)
                if branch_of(row.get("branch")) == name]
    for row in attached:
        path = str(Path(row["path"]).resolve())
        if path in busy_paths or path not in managed_paths or row.get("locked"):
            return "retryable", f"landed branch has a busy or unmanaged worktree: {path}"
        if row.get("head") != expected_sha:
            return "conflict", f"worktree head moved after delivery: {path}"
        if await git.ahas_uncommitted_changes(path, strict=True) is not False:
            return "retryable", f"landed branch worktree is dirty or unreadable: {path}"
    for row in attached:
        # Preserve the entire checkout at the same commit. Never reset, clean
        # or force-remove a worktree merely to make its branch deletable.
        if (await git.aget_current_branch(row["path"]) != name
                or await git.arev_parse(row["path"], "HEAD") != expected_sha):
            return "conflict", "worktree changed before detachment"
        if await git.ahas_uncommitted_changes(row["path"], strict=True) is not False:
            return "retryable", "worktree changed before detachment"
        await _run(git, row["path"], "switch", "--detach", expected_sha)
    if any(branch_of(row.get("branch")) == name for row in await git.aworktree_list(checkout)):
        return "retryable", "landed branch is still checked out"
    if exists:
        await git.adelete_local_ref_exact(checkout, ref=ref, expected_old_oid=expected_sha)
    if await git.aref_exists(checkout, ref) is not False:
        return "retryable", "local branch deletion is unconfirmed"
    if await git.aref_exists(checkout, tracking) is True:
        current = await git.arev_parse(checkout, tracking)
        if current != expected_sha:
            return "conflict", "origin tracking ref moved after delivery"
        await _run(git, checkout, "update-ref", "-d", tracking, expected_sha)
    if await git.aref_exists(checkout, tracking) is not False:
        return "retryable", "origin tracking ref deletion is unconfirmed"
    return "complete", None


def _append_backup(
    path: Path,
    *,
    repository: str,
    scope: str,
    branch: str,
    sha: str,
    proof: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if new_file:
            handle.write("repository\tscope\tref\tsha\tproof\trecorded_at_utc\n")
        handle.write(
            f"{repository}\t{scope}\trefs/heads/{branch}\t{sha}\t{proof}\t"
            f"{datetime.now(UTC).isoformat()}\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


async def sweep_checkout(
    git: GitManager,
    checkout: str,
    *,
    repository_url: str,
    default_branch: str,
    holds: dict[str, str],
    backup_path: Path,
) -> BranchSweepReport:
    """Sweep one git common directory after fetching its authorized origin."""
    report = BranchSweepReport(checkout=checkout)
    await git.afetch_origin(checkout, repository_url=repository_url, all_heads=True)
    remote_before = await remote_heads(_run_adapter(git), checkout)
    local_rows = await _run(
        git, checkout, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"
    )
    local_before = {
        name: sha
        for line in local_rows.splitlines()
        if len((parts := line.split())) == 2
        for name, sha in [parts]
    }
    report.remote_before = len(remote_before)
    report.local_before = len(local_before)

    # Prune only missing-directory registrations. Existing worktree paths are
    # never removed here; the slot reaper owns that liveness decision.
    stale = await _run(git, checkout, "worktree", "prune", "--dry-run", "--verbose")
    report.stale_worktrees_pruned = sum(
        line.lstrip().startswith("Removing ") for line in stale.splitlines()
    )
    await git.aworktree_prune(checkout)

    stash_output = await _run(git, checkout, "stash", "list", "--format=%gd %H %s")
    report.stashes = [line for line in stash_output.splitlines() if line.strip()]

    worktrees = await git.aworktree_list(checkout)
    attached = {row["branch"] for row in worktrees if row.get("branch")}
    protected = PROTECTED_NAMES | PROTECTED_BRANCHES | {default_branch}
    target_names = ("dev", "main", default_branch)
    targets = {
        name: remote_before[name]
        for name in dict.fromkeys(target_names)
        if name in remote_before
    }
    # No target means no proof; in particular, do not infer safety from a
    # checkout's potentially stale local branch names.
    if not targets:
        raise GitError("origin has no dev, main or configured default branch")

    local_proofs: dict[str, str] = {}
    remote_proofs: dict[str, str] = {}
    all_names = set(local_before) | set(remote_before)
    for branch in sorted(all_names):
        if branch in protected or branch.startswith("aq-provenance/"):
            continue
        if branch in holds or branch in attached:
            if branch in local_before:
                report.local_held += 1
            if branch in remote_before:
                report.remote_held += 1
            continue
        local_sha = local_before.get(branch)
        if local_sha:
            proof = await _proof(git, checkout, local_sha, targets)
            if proof:
                local_proofs[branch] = proof
            else:
                report.unique_unmerged.append((branch, local_sha))
                if branch.startswith("aq/preserved/"):
                    report.preserved_kept.append((branch, local_sha))
        remote_sha = remote_before.get(branch)
        if remote_sha:
            proof = await _proof(git, checkout, remote_sha, targets)
            if proof:
                remote_proofs[branch] = proof
            else:
                report.unique_unmerged.append((branch, remote_sha))
                if branch.startswith("aq/preserved/"):
                    report.preserved_kept.append((branch, remote_sha))

    for branch, proof in sorted(local_proofs.items()):
        if branch in attached:
            continue
        sha = local_before[branch]
        _append_backup(
            backup_path, repository=repository_url, scope="local",
            branch=branch, sha=sha, proof=proof,
        )
        try:
            await git.adelete_local_ref_exact(
                checkout, ref=f"refs/heads/{branch}", expected_old_oid=sha
            )
        except GitError:
            # The compare-and-delete failed because the ref moved; keep it.
            continue
        report.local_deleted.append((branch, sha, proof))

    for branch, proof in sorted(remote_proofs.items()):
        sha = remote_before[branch]
        current = await git.als_remote_ref(checkout, branch)
        if current.state is RemoteRefState.ERROR or current.state is RemoteRefState.ABSENT:
            continue
        if current.oid != sha:
            continue
        _append_backup(
            backup_path, repository=repository_url, scope="remote",
            branch=branch, sha=sha, proof=proof,
        )
        try:
            await git.adelete_remote_ref_exact(checkout, branch, sha)
        except GitError:
            # Resolve moved/uncertain deletes with an exact ref read below.
            pass
        after = await git.als_remote_ref(checkout, branch)
        if after.state is RemoteRefState.ABSENT:
            report.remote_deleted.append((branch, sha, proof))

    await git.afetch_origin(checkout, repository_url=repository_url, all_heads=True)
    final_remote = await remote_heads(_run_adapter(git), checkout)
    final_local_rows = await _run(
        git, checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    )
    report.remote_after = len(final_remote)
    report.local_after = len([line for line in final_local_rows.splitlines() if line.strip()])
    return report


__all__ = ["BranchSweepReport", "clean_landed_checkout", "sweep_checkout"]
