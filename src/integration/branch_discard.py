"""Remove task branches an operator explicitly asked to discard.

Deleting a task in a hierarchy/train project may also remove the branch that
task put on the remote, but only on an explicit choice — see
``docs/superpowers/specs/2026-09-08-task-deletion-with-materialized-branches-design.md``.
``guard_integration_mutation`` records that choice by retiring the origin and
marking it ``discard_state='pending'``; this service is what actually deletes
the ref.

The work is deliberately *not* on the integration outbox.  That outbox
dispatches only to playbooks (``Orchestrator.accept_integration_event``), and
no shipped playbook consumes branch-level events — the sibling
``integration.branch_materialization_pending`` event has no consumer at all
(design §6).  A drain over the origin rows is durable in the same way and
actually runs.

Origin rows carry no foreign key to ``tasks`` and ``_delete_one`` leaves them
alone, so a retired origin outlives the task it describes and this service can
still find its work after the subtree is gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select, update

from src.database.tables import integration_branch_owners, task_branch_origins
from src.git.github_contracts import GitHubRepositoryBinding
from src.git.manager import GitError

logger = logging.getLogger(__name__)

#: A discard that keeps failing backs off from ~30s toward this ceiling.
RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 3600.0
#: Transport trouble is retried this many times before the row is parked as
#: ``failed`` for an operator.  Conflicts never retry — they are a statement
#: about the remote, not about the transport.
MAX_ATTEMPTS = 8

#: Temporary refs the bundle step pins the doomed heads under.  The bundle is
#: the durable copy; the refs themselves are deleted in the same step, so
#: nothing lingers in the retained store.
BACKUP_REF_PREFIX = "refs/aq-backup/heads/"


def _month(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m")


async def _bundle(
    run_git, store, heads: dict[str, str], *, main_head, backup_dir, repository_id, now
) -> Path:
    """Write *heads* to a new verified bundle; raise rather than return an unproved one."""
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%S%fZ")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", repository_id) or "repository"
    directory = Path(backup_dir) / _month(now)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}-{slug}.bundle"
    refs = {f"{BACKUP_REF_PREFIX}{branch}": sha for branch, sha in heads.items()}
    try:
        for ref, sha in refs.items():
            await run_git(store, "update-ref", ref, sha)
        await run_git(store, "bundle", "create", str(path), *sorted(refs), "--not", main_head)
        await run_git(store, "bundle", "verify", str(path))
        listed = await run_git(store, "bundle", "list-heads", str(path))
    finally:
        for ref in refs:
            try:
                await run_git(store, "update-ref", "-d", ref)
            except GitError:
                logger.warning("could not remove temporary backup ref %s", ref)
    present = {tuple(line.split(" ", 1)) for line in listed.splitlines() if " " in line}
    missing = sorted(ref for ref, sha in refs.items() if (sha, ref) not in present)
    if missing:
        raise GitError(f"branch backup {path} is missing {missing}")
    return path


def _record_deletions(
    backup_dir, targets, *, bundled, bundle, repository_id, now
) -> Path:
    """Append one line per branch to the month's deletion log, durably."""
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_month(now)}.tsv"
    recorded_at = datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")

    def clean(value) -> str:
        return re.sub(r"[\t\r\n]+", " ", str(value))

    lines = [
        "\t".join((
            branch,
            targets[branch]["head"],
            clean(targets[branch].get("reason") or "-"),
            str(bundle) if branch in bundled else "-",
            recorded_at,
            clean(repository_id),
        ))
        + "\n"
        for branch in sorted(targets)
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    return path


class BranchDiscardResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal["complete", "conflict", "failed", "retryable", "wait", "stale"]
    origin_id: str
    task_id: str
    branch: str
    attempts: int
    error: str | None = None


class BranchDiscardService:
    """Drain ``task_branch_origins`` rows marked for branch discard."""

    def __init__(
        self,
        db: Any,
        *,
        data_dir: str | Path,
        git_manager: Any | None = None,
        app_client_factory: Any | None = None,
        repository_binding_resolver: Any | None = None,
        clock=time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager
        self.app_client_factory = app_client_factory
        self.repository_binding_resolver = repository_binding_resolver
        self.clock = clock

    # -- draining -------------------------------------------------------

    async def drain_due(self, *, now: float | None = None, limit: int = 20) -> list[
        BranchDiscardResult
    ]:
        """Advance every discard whose backoff has elapsed, oldest request first."""
        observed_at = self.clock() if now is None else now
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(task_branch_origins)
                        .where(
                            task_branch_origins.c.discard_state == "pending",
                            or_(
                                task_branch_origins.c.discard_next_attempt_at.is_(None),
                                task_branch_origins.c.discard_next_attempt_at <= observed_at,
                            ),
                        )
                        .order_by(
                            task_branch_origins.c.discard_requested_at,
                            task_branch_origins.c.id,
                        )
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        results = []
        for row in rows:
            results.append(await self.advance(row["id"], now=observed_at))
        return results

    async def advance(self, origin_id: str, *, now: float | None = None) -> BranchDiscardResult:
        """Attempt one discard, claiming it by its attempt count."""
        observed_at = self.clock() if now is None else now
        async with self.db.immediate() as conn:
            row = (
                (
                    await conn.execute(
                        select(task_branch_origins)
                        .where(task_branch_origins.c.id == origin_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return BranchDiscardResult(
                    outcome="stale", origin_id=origin_id, task_id="", branch="", attempts=0
                )
            if row["discard_state"] != "pending":
                return self._result("stale", row)
            if (
                row["discard_next_attempt_at"] is not None
                and float(row["discard_next_attempt_at"]) > observed_at
            ):
                return self._result("wait", row)
            # Claim by attempt count: two daemons draining the same row cannot
            # both reach the delete.
            claimed = await conn.execute(
                update(task_branch_origins)
                .where(
                    task_branch_origins.c.id == origin_id,
                    task_branch_origins.c.discard_state == "pending",
                    task_branch_origins.c.discard_attempts == row["discard_attempts"],
                )
                .values(
                    discard_attempts=int(row["discard_attempts"]) + 1,
                    discard_next_attempt_at=observed_at
                    + self._backoff(int(row["discard_attempts"]) + 1),
                )
            )
            if claimed.rowcount != 1:
                return self._result("wait", row)
            claimed_row = dict(row)
            claimed_row["discard_attempts"] = int(row["discard_attempts"]) + 1

        try:
            outcome, error = await self._perform(claimed_row)
        except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - transport trouble is retryable by default
            outcome, error = "retryable", str(exc) or type(exc).__name__
        return await self._finalize(claimed_row, observed_at, outcome, error)

    # -- the actual removal ---------------------------------------------

    async def _perform(self, row: dict[str, Any]) -> tuple[str, str | None]:
        branch = self._branch(row)
        repository = await self.db.get_repo(row["repository_id"])
        if repository is None:
            return "failed", "discard repository is unavailable"
        if branch == repository.default_branch:
            # Unreachable through the guard (a task branch is always
            # ``aq/<task-id>``), and catastrophic if it ever became reachable.
            return "conflict", "default branch discard is forbidden"
        owner = await self._live_owner(row["repository_id"], branch)
        if owner is not None:
            # Something re-acquired the ref after the delete — a repair
            # delegate or a verifier.  Deleting it under a live writer would
            # lose work the operator never asked to discard.
            return "conflict", "branch has an active owner"
        binding = await self._binding(repository)
        if binding is None:
            return "retryable", "repository binding is unavailable"
        client = await self._app_client(binding)
        if client is None or self.git is None:
            return "retryable", "authenticated discard transport is unavailable"
        token = await client.installation_token()
        head = await client.exact_head_ref(branch)
        if head is None:
            return "complete", None
        main_head = await client.exact_head_ref(repository.default_branch)
        if not main_head:
            # Cannot assess reachability without the default branch head, and a
            # thin ``--not <main>`` bundle needs it; refuse rather than guess.
            return "retryable", f"cannot back up without the {repository.default_branch} head"
        await self._backup_before_delete(
            row,
            binding=binding,
            token=token,
            branch=branch,
            head=head,
            main_head=main_head,
        )
        try:
            await self.git.adelete_ref_with_app_auth(
                str(self.retained_store(row["repository_id"])),
                repository=binding,
                token=token,
                branch=branch,
                expected_old_oid=head,
            )
        except GitError:
            observed = await client.exact_head_ref(branch)
            if observed is None:
                return "complete", None
            if observed != head:
                return "conflict", "branch moved during discard"
            raise
        return "complete", None

    async def _live_owner(self, repository_id: str, branch: str):
        async with self.db._engine.connect() as conn:
            return (
                (
                    await conn.execute(
                        select(integration_branch_owners.c.id).where(
                            integration_branch_owners.c.repository_id == repository_id,
                            integration_branch_owners.c.ref == branch,
                            integration_branch_owners.c.handoff_state != "released",
                        )
                    )
                )
                .mappings()
                .first()
            )

    async def _binding(self, repository) -> GitHubRepositoryBinding | None:
        if self.repository_binding_resolver is None:
            return None
        binding = self.repository_binding_resolver(repository)
        if inspect.isawaitable(binding):
            binding = await binding
        return binding

    async def _app_client(self, binding: GitHubRepositoryBinding):
        if self.app_client_factory is None:
            return None
        client = self.app_client_factory(binding)
        if inspect.isawaitable(client):
            client = await client
        if client is None or client.repository != binding:
            return None
        return client

    # -- the pre-delete backup ------------------------------------------

    async def _backup_before_delete(
        self,
        row: dict[str, Any],
        *,
        binding: GitHubRepositoryBinding,
        token: str,
        branch: str,
        head: str,
        main_head: str,
    ) -> None:
        """Bundle a doomed head and record the row *before* the ref is deleted.

        Nothing is deleted that cannot be put back: the head (and its commits
        not on the default branch) is written to a verified bundle under
        ``<data_dir>/backups/branch-deletions/`` and a TSV log line is
        appended.  Any failure raises, which parks the discard as retryable;
        the delete must not run without a restorable copy.
        """
        repository_id = row["repository_id"]
        now = self.clock()
        store = self.retained_store(repository_id)
        await self._ensure_store(store)
        # The retained store may not hold the head or the default-branch head
        # yet; both must be local before anything can be proven, bundled or
        # deleted against.
        await self.git.afetch_exact_oid_with_app_auth(
            str(store),
            repository=binding,
            token=token,
            oid=head,
            destination_ref=f"refs/aq/discard-backup/heads/{branch}",
        )
        await self.git.afetch_exact_oid_with_app_auth(
            str(store),
            repository=binding,
            token=token,
            oid=main_head,
            destination_ref="refs/aq/discard-backup/default",
        )
        bundle = None
        if not await self.git.ais_ancestor(str(store), head, main_head):
            # ``False`` covers "already on the default branch" and "could not
            # be established"; bundling the unknown case is the one that keeps
            # the deletion restorable.
            bundle = await _bundle(
                self._run_git,
                store,
                {branch: head},
                main_head=main_head,
                backup_dir=self.backup_dir,
                repository_id=repository_id,
                now=now,
            )
        _record_deletions(
            self.backup_dir,
            {branch: {"head": head, "reason": f"task {row['task_id']} discarded"}},
            bundled={branch} if bundle is not None else set(),
            bundle=bundle,
            repository_id=repository_id,
            now=now,
        )

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups" / "branch-deletions"

    async def _run_git(self, store: Path, *args: str) -> str:
        result = await self.git.arun_git_result(list(args), cwd=str(store))
        if result.returncode:
            raise GitError(result.stderr or result.stdout or "Git command failed")
        return result.stdout.strip()

    async def _ensure_store(self, store: Path) -> None:
        store.parent.mkdir(parents=True, exist_ok=True)
        if not store.exists():
            result = await self.git.arun_git_result(
                ["init", "--bare", "--template=", str(store)], cwd=str(store.parent)
            )
            if result.returncode != 0:
                raise GitError(result.stderr or "retained store initialization failed")

    # -- bookkeeping ----------------------------------------------------

    async def _finalize(
        self, row: dict[str, Any], now: float, outcome: str, error: str | None
    ) -> BranchDiscardResult:
        attempts = int(row["discard_attempts"])
        if outcome == "retryable" and attempts >= MAX_ATTEMPTS:
            outcome, error = "failed", error or "discard exhausted its attempts"
        if outcome == "retryable":
            state, next_attempt = "pending", now + self._backoff(attempts)
        else:
            state, next_attempt = outcome, None
        async with self.db.immediate() as conn:
            await conn.execute(
                update(task_branch_origins)
                .where(
                    task_branch_origins.c.id == row["id"],
                    task_branch_origins.c.discard_attempts == attempts,
                )
                .values(
                    discard_state=state,
                    discard_next_attempt_at=next_attempt,
                    discard_last_error=error,
                )
            )
        if outcome in {"conflict", "failed"}:
            logger.warning(
                "Branch discard for %s (%s) ended %s: %s",
                self._branch(row), row["id"], outcome, error,
            )
        return self._result(outcome, row, error=error)

    @staticmethod
    def _backoff(attempts: int) -> float:
        return min(RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)), RETRY_MAX_SECONDS)

    @staticmethod
    def _branch(row: dict[str, Any]) -> str:
        return f"aq/{row['task_id']}"

    def _result(
        self, outcome: str, row: dict[str, Any], *, error: str | None = None
    ) -> BranchDiscardResult:
        return BranchDiscardResult(
            outcome=outcome,
            origin_id=row["id"],
            task_id=row["task_id"],
            branch=self._branch(row),
            attempts=int(row["discard_attempts"]),
            error=error,
        )

    def retained_store(self, repository_id: str) -> Path:
        digest = hashlib.sha256(repository_id.encode()).hexdigest()
        return self.data_dir / "integration-repositories" / f"{digest}.git"


__all__ = ["BranchDiscardResult", "BranchDiscardService"]
