"""Ordered, restartable construction of sealed root integration candidates."""

from __future__ import annotations

import hashlib
import inspect
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_candidate_member_results,
    integration_candidate_revisions,
    integration_repair_operations,
    integration_repair_stages,
    project_integration_leases,
    projects,
)
from src.git.manager import GitManager, RemoteRefState, is_valid_git_oid
from src.integration.repair import RepairService


class CandidateBuildResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal[
        "empty",
        "built",
        "already_built",
        "conflict",
        "source_moved",
        "base_moved",
        "stale_revision",
        "wait",
        "human_required",
    ]
    batch_id: str
    revision: int
    operation_id: str | None = None
    head_sha: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    member_ordinal: int | None = None


class AuditPullRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    url: str
    number: int
    head_sha: str
    head_branch: str
    base_branch: str


class CandidateRepairLineage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    batch_id: str
    revision: int
    member_ordinal: int
    operation_id: str
    operation_stage: int
    partial_head_sha: str
    source_base_sha: str
    source_head_sha: str
    resolved_head_sha: str
    repair_commit_shas: tuple[str, ...]


class CandidateRepairResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal["accepted", "already_accepted", "stale"]
    batch_id: str
    revision: int
    member_ordinal: int


class AuditForgeProvider(Protocol):
    async def ensure_audit_pr(
        self,
        *,
        repository_id: str,
        branch: str,
        head_sha: str,
        base_branch: str,
        batch_id: str,
    ) -> AuditPullRequest: ...


CrashHook = Callable[[str], Awaitable[None] | None]
RepositoryResolver = Callable[[str], Awaitable[Any] | Any]
_COAUTHOR_RE = re.compile(r"(?im)^co-authored-by:\s*(?P<name>[^<\n]+?)\s*<(?P<email>[^>\n]+)>\s*$")


class CandidateService:
    """Build every immutable member into one candidate revision."""

    def __init__(
        self,
        db,
        *,
        data_dir: str | Path,
        git_manager: GitManager | None = None,
        repository_resolver: RepositoryResolver | None = None,
        forge_provider: AuditForgeProvider | None = None,
        app_client: Any | None = None,
        crash_hook: CrashHook | None = None,
        repair_service: RepairService | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager or GitManager()
        self.repository_resolver = repository_resolver
        self.forge_provider = forge_provider
        self.app_client = app_client
        self.crash_hook = crash_hook
        self.repair = repair_service or RepairService(db)
        self.clock = clock

    async def build(self, batch_id: str) -> CandidateBuildResult:
        state = await self._locked_state(batch_id)
        batch = state["batch"]
        if batch["lifecycle"] == "empty":
            return CandidateBuildResult(
                outcome="empty", batch_id=batch_id, revision=int(batch["current_revision"])
            )
        revision_number = int(batch["current_revision"])
        revision = await self._ensure_revision(state, revision_number, batch["base_sha"])
        was_built = revision["state"] == "built"
        operation_id = state["operation"]["id"]
        if revision.get("repair_parent_revision") is None:
            started = await self.repair.start(
                operation_id,
                revision["construction_base_sha"],
                batch_id,
                now=self.clock(),
            )
            if started["outcome"] not in {"started", "already_started"}:
                raise ValueError("candidate repair budget could not be activated")
        repository = await self._repository(state["batch"]["repository_id"])
        store = await self._ensure_store(repository)
        await self._fetch(store, repository.url)
        await self._retain_sources(store, state)
        revision = await self._construct(state, revision, store, operation_id=operation_id)
        if revision["state"] in {"conflict", "source_moved", "base_moved"}:
            return self._result(revision["state"], state, revision, operation_id)
        outcome = "already_built" if was_built or batch["pr_url"] else "built"
        pushed = await self._publish(state, revision, store)
        return self._result(outcome, state, pushed, operation_id)

    async def rebuild(
        self, batch_id: str, expected_revision: int, new_base_sha: str
    ) -> CandidateBuildResult:
        if not is_valid_git_oid(new_base_sha):
            raise ValueError("candidate rebuild base must be an exact Git OID")
        state = await self._locked_state(batch_id)
        batch = state["batch"]
        if int(batch["current_revision"]) != expected_revision:
            return CandidateBuildResult(
                outcome="stale_revision",
                batch_id=batch_id,
                revision=int(batch["current_revision"]),
                operation_id=state["operation"]["id"],
            )
        repository = await self._repository(batch["repository_id"])
        store = await self._ensure_store(repository)
        current = await self._revision(batch_id, expected_revision)
        if current is None or not current.get("head_sha"):
            raise ValueError("current candidate revision is not recoverable")
        await self._pin(store, self._recovery_ref(batch_id, expected_revision), current["head_sha"])
        await self._crash("after_superseded_pin")
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch["project_id"])
            locked = (
                (
                    await conn.execute(
                        select(integration_batches)
                        .where(integration_batches.c.id == batch_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if int(locked["current_revision"]) != expected_revision:
                return CandidateBuildResult(
                    outcome="stale_revision",
                    batch_id=batch_id,
                    revision=int(locked["current_revision"]),
                    operation_id=state["operation"]["id"],
                )
            await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == batch_id,
                    integration_candidate_revisions.c.revision == expected_revision,
                )
                .values(state="superseded", updated_at=now)
            )
            next_revision = expected_revision + 1
            await conn.execute(
                insert(integration_candidate_revisions).values(
                    batch_id=batch_id,
                    revision=next_revision,
                    construction_base_sha=new_base_sha,
                    next_member_ordinal=0,
                    repair_parent_revision=expected_revision,
                    head_sha=new_base_sha,
                    state="constructing",
                    created_at=now,
                    updated_at=now,
                )
            )
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == batch_id)
                .values(current_revision=next_revision, tested_candidate_sha=None, updated_at=now)
            )
            binding = await self.repair.bind_current_batch_subject_on(
                conn, state["operation"]["id"], now=now
            )
        if binding["deadline_due"]:
            expired = await self.repair.expire(state["operation"]["id"], binding["stage"], now=now)
            if expired["action"] == "block_for_human":
                return CandidateBuildResult(
                    outcome="human_required",
                    batch_id=batch_id,
                    revision=next_revision,
                    operation_id=state["operation"]["id"],
                )
            if expired["action"] == "dispatch_debug":
                async with self.db.immediate() as conn:
                    await self.db.lock_hierarchy_project(conn, batch["project_id"])
                    await conn.execute(
                        update(integration_batches)
                        .where(integration_batches.c.id == batch_id)
                        .values(lifecycle="repairing", updated_at=now)
                    )
                dispatched = await self.repair.dispatch(
                    state["operation"]["id"], int(expired["stage"])
                )
                if dispatched["outcome"] == "human_required":
                    return CandidateBuildResult(
                        outcome="human_required",
                        batch_id=batch_id,
                        revision=next_revision,
                        operation_id=state["operation"]["id"],
                    )
                if dispatched["outcome"] in {"busy", "configuration_blocked"}:
                    return CandidateBuildResult(
                        outcome="wait",
                        batch_id=batch_id,
                        revision=next_revision,
                        operation_id=state["operation"]["id"],
                    )
        return await self.build(batch_id)

    async def accept_repair(self, lineage: CandidateRepairLineage) -> CandidateRepairResult:
        for oid in (
            lineage.partial_head_sha,
            lineage.source_base_sha,
            lineage.source_head_sha,
            lineage.resolved_head_sha,
            *lineage.repair_commit_shas,
        ):
            if not is_valid_git_oid(oid):
                raise ValueError("repair lineage contains a non-OID")
        state = await self._locked_state(lineage.batch_id)
        if (
            int(state["batch"]["current_revision"]) != lineage.revision
            or state["operation"]["id"] != lineage.operation_id
        ):
            return self._repair_result("stale", lineage)
        row = await self._member_result(lineage.batch_id, lineage.revision, lineage.member_ordinal)
        if row is None:
            return self._repair_result("stale", lineage)
        exact = lineage.model_dump(mode="json")
        evidence = row.get("conflict_evidence") or {}
        if row["result"] == "applied":
            outcome = (
                "already_accepted"
                if evidence.get("accepted_lineage") == exact
                and row["generated_squash_sha"] == lineage.resolved_head_sha
                else "stale"
            )
            return self._repair_result(outcome, lineage)
        expected = {
            "batch_id": lineage.batch_id,
            "revision": lineage.revision,
            "ordinal": lineage.member_ordinal,
            "operation_id": lineage.operation_id,
            "operation_stage": lineage.operation_stage,
            "partial_head_sha": lineage.partial_head_sha,
            "source_base_sha": lineage.source_base_sha,
            "source_head_sha": lineage.source_head_sha,
        }
        if row["result"] != "conflict" or any(
            evidence.get(key) != value for key, value in expected.items()
        ):
            return self._repair_result("stale", lineage)
        repository = await self._repository(state["batch"]["repository_id"])
        store = await self._ensure_store(repository)
        if not await self._valid_repair_lineage(store, lineage):
            return self._repair_result("stale", lineage)
        await self._pin(
            store,
            self._recovery_ref(lineage.batch_id, lineage.revision),
            lineage.resolved_head_sha,
        )
        await self._crash("after_repair_pin")
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            stage = (
                (
                    await conn.execute(
                        select(integration_repair_stages).where(
                            integration_repair_stages.c.operation_id == lineage.operation_id,
                            integration_repair_stages.c.ordinal == lineage.operation_stage,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            current = (
                (
                    await conn.execute(
                        select(integration_candidate_member_results).where(
                            integration_candidate_member_results.c.batch_id == lineage.batch_id,
                            integration_candidate_member_results.c.revision == lineage.revision,
                            integration_candidate_member_results.c.member_ordinal
                            == lineage.member_ordinal,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                stage is None
                or stage["state"] not in {"active", "awaiting_completion"}
                or current is None
                or current["result"] != "conflict"
            ):
                return self._repair_result("stale", lineage)
            await conn.execute(
                update(integration_candidate_member_results)
                .where(
                    integration_candidate_member_results.c.batch_id == lineage.batch_id,
                    integration_candidate_member_results.c.revision == lineage.revision,
                    integration_candidate_member_results.c.member_ordinal == lineage.member_ordinal,
                )
                .values(
                    result="applied",
                    generated_squash_sha=lineage.resolved_head_sha,
                    conflict_evidence={**evidence, "accepted_lineage": exact},
                    updated_at=now,
                )
            )
            await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == lineage.batch_id,
                    integration_candidate_revisions.c.revision == lineage.revision,
                    integration_candidate_revisions.c.next_member_ordinal == lineage.member_ordinal,
                )
                .values(
                    next_member_ordinal=lineage.member_ordinal + 1,
                    head_sha=lineage.resolved_head_sha,
                    updated_at=now,
                )
            )
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == lineage.batch_id)
                .values(lifecycle="building", updated_at=now)
            )
        return self._repair_result("accepted", lineage)

    async def _locked_state(self, batch_id: str) -> dict[str, Any]:
        async with self.db.immediate() as conn:
            batch = (
                (
                    await conn.execute(
                        select(integration_batches)
                        .where(integration_batches.c.id == batch_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if batch is None:
                raise ValueError("integration batch does not exist")
            project = (
                (await conn.execute(select(projects).where(projects.c.id == batch["project_id"])))
                .mappings()
                .one_or_none()
            )
            if project is None:
                raise ValueError("integration batch project does not exist")
            await self.db.lock_hierarchy_project(conn, project["id"])
            if (
                project["hierarchical_integration_mode"] != "train"
                or project["integration_repository_id"] != batch["repository_id"]
            ):
                raise ValueError("integration batch is outside active train authority")
            members = (
                (
                    await conn.execute(
                        select(integration_batch_members)
                        .where(integration_batch_members.c.batch_id == batch_id)
                        .order_by(integration_batch_members.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            if batch["lifecycle"] != "empty" and [int(row["ordinal"]) for row in members] != list(
                range(len(members))
            ):
                raise ValueError("integration batch member ordinals are incomplete")
            lease = (
                (
                    await conn.execute(
                        select(project_integration_leases).where(
                            project_integration_leases.c.project_id == project["id"]
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if batch["lifecycle"] != "empty" and (
                lease is None
                or lease["batch_id"] != batch_id
                or lease["repository_id"] != batch["repository_id"]
            ):
                raise ValueError("integration batch is not the current project train")
            operation = (
                (
                    await conn.execute(
                        select(integration_repair_operations).where(
                            integration_repair_operations.c.batch_id == batch_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if batch["lifecycle"] != "empty" and operation is None:
                raise ValueError("integration batch operation is missing")
            return {
                "batch": dict(batch),
                "project": dict(project),
                "members": [dict(row) for row in members],
                "operation": dict(operation) if operation else None,
            }

    async def _ensure_revision(self, state, revision: int, base_sha: str) -> dict[str, Any]:
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            row = (
                (
                    await conn.execute(
                        select(integration_candidate_revisions).where(
                            integration_candidate_revisions.c.batch_id == state["batch"]["id"],
                            integration_candidate_revisions.c.revision == revision,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                await conn.execute(
                    insert(integration_candidate_revisions).values(
                        batch_id=state["batch"]["id"],
                        revision=revision,
                        construction_base_sha=base_sha,
                        next_member_ordinal=0,
                        head_sha=base_sha,
                        state="constructing",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await conn.execute(
                    update(integration_batches)
                    .where(integration_batches.c.id == state["batch"]["id"])
                    .values(lifecycle="building", updated_at=now)
                )
                row = {
                    "batch_id": state["batch"]["id"],
                    "revision": revision,
                    "construction_base_sha": base_sha,
                    "next_member_ordinal": 0,
                    "head_sha": base_sha,
                    "state": "constructing",
                }
            return dict(row)

    async def _construct(self, state, revision, store: Path, *, operation_id: str):
        batch_id = state["batch"]["id"]
        current = revision["head_sha"] or revision["construction_base_sha"]
        if not await self._commit_exists(store, revision["construction_base_sha"]):
            return {**revision, "state": "base_moved", "head_sha": None}
        for member in state["members"]:
            ordinal = int(member["ordinal"])
            if ordinal < int(revision["next_member_ordinal"]):
                continue
            existing = await self._member_result(batch_id, int(revision["revision"]), ordinal)
            if existing and existing["result"] == "applied":
                current = existing["generated_squash_sha"]
                continue
            if existing and existing["result"] == "conflict":
                evidence = existing["conflict_evidence"]
                await self.repair.dispatch(operation_id, 0)
                return {
                    **revision,
                    "state": "conflict",
                    "head_sha": evidence["partial_head_sha"],
                    "member_ordinal": ordinal,
                }
            if not await self._member_identity_matches(store, member):
                return {
                    **revision,
                    "state": "source_moved",
                    "head_sha": current,
                    "member_ordinal": ordinal,
                }
            await self._pending(state, revision, member)
            await self._crash("before_member_mutation")
            if await self.git.areserved_paths_in_diff(
                str(store), member["source_base_sha"], member["reviewed_head_sha"]
            ):
                return await self._conflict(
                    state, revision, member, current, "reserved_path", operation_id
                )
            parent_repair = await self._accepted_parent_repair(revision, ordinal)
            if parent_repair is None:
                merge_args = [
                    "merge-tree",
                    "--write-tree",
                    current,
                    member["reviewed_head_sha"],
                ]
            else:
                accepted = parent_repair["accepted_lineage"]
                merge_args = [
                    "merge-tree",
                    "--write-tree",
                    f"--merge-base={accepted['partial_head_sha']}",
                    current,
                    accepted["resolved_head_sha"],
                ]
            tree = await self.git.arun_git_result(merge_args, cwd=str(store))
            if tree.returncode != 0:
                return await self._conflict(
                    state, revision, member, current, tree.stdout or tree.stderr, operation_id
                )
            tree_sha = tree.stdout.splitlines()[0].strip()
            authors = await self._authors(
                store, member["source_base_sha"], member["reviewed_head_sha"]
            )
            message = self._message(state, member, authors, parent_repair)
            primary = (
                authors[0]
                if authors
                else {
                    "name": "Agent Queue Integration",
                    "email": "integration@agent-queue.local",
                }
            )
            authored_at = f"@{int(state['batch']['created_at'])} +0000"
            committed = await self.git.arun_git_result(
                ["commit-tree", tree_sha, "-p", current, "-m", message],
                cwd=str(store),
                env={
                    "GIT_AUTHOR_NAME": primary["name"],
                    "GIT_AUTHOR_EMAIL": primary["email"],
                    "GIT_COMMITTER_NAME": "Agent Queue Integration",
                    "GIT_COMMITTER_EMAIL": "integration@agent-queue.local",
                    "GIT_AUTHOR_DATE": authored_at,
                    "GIT_COMMITTER_DATE": authored_at,
                },
            )
            if committed.returncode != 0:
                raise RuntimeError(committed.stderr or "candidate commit failed")
            current = committed.stdout.strip()
            await self._pin(store, self._recovery_ref(batch_id, int(revision["revision"])), current)
            await self._crash("after_member_mutation")
            revision = await self._applied(
                state, revision, member, current, parent_repair=parent_repair
            )
            await self._crash("after_member_progress")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == batch_id,
                    integration_candidate_revisions.c.revision == revision["revision"],
                )
                .values(state="built", head_sha=current, updated_at=self.clock())
            )
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == batch_id)
                .values(lifecycle="testing", updated_at=self.clock())
            )
        return {**revision, "state": "built", "head_sha": current}

    async def _publish(self, state, revision, store):
        if self.app_client is None or self.forge_provider is None:
            return revision
        batch = state["batch"]
        if batch.get("pr_url"):
            return {**revision, "pr_url": batch["pr_url"]}
        branch = batch["integration_branch"].removeprefix("refs/heads/")
        token = await self.app_client.installation_token()
        remote = await self.git.als_remote_ref(str(store), branch)
        if remote.state is RemoteRefState.ERROR:
            raise RuntimeError(remote.error or "candidate remote ref read failed")
        if remote.state is RemoteRefState.ABSENT:
            expected = "0" * 40
        elif remote.oid == revision["head_sha"]:
            expected = None
        else:
            previous = await self._revision(batch["id"], int(revision["revision"]) - 1)
            if previous is None or remote.oid != previous.get("head_sha"):
                raise ValueError("integration candidate ref moved outside its lease")
            expected = remote.oid
        if expected is not None:
            await self.git.apush_oid_with_app_auth(
                str(store),
                repository=self.app_client.repository,
                token=token,
                tip_oid=revision["head_sha"],
                branch=branch,
                expected_old_oid=expected,
            )
            await self._crash("after_candidate_push")
        pr = await self.forge_provider.ensure_audit_pr(
            repository_id=batch["repository_id"],
            branch=branch,
            head_sha=revision["head_sha"],
            base_branch=(await self._repository(batch["repository_id"])).default_branch,
            batch_id=batch["id"],
        )
        await self._crash("after_audit_pr_create")
        if pr.head_sha != revision["head_sha"] or pr.head_branch != branch:
            raise ValueError("audit PR identity does not match candidate")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch["project_id"])
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == batch["id"])
                .values(pr_url=pr.url, updated_at=self.clock())
            )
        await self._crash("after_audit_pr_write")
        return {**revision, "pr_url": pr.url}

    async def _pending(self, state, revision, member):
        if await self._member_result(state["batch"]["id"], revision["revision"], member["ordinal"]):
            return
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await conn.execute(
                insert(integration_candidate_member_results).values(
                    batch_id=state["batch"]["id"],
                    revision=revision["revision"],
                    member_ordinal=member["ordinal"],
                    input_head_sha=member["reviewed_head_sha"],
                    input_tree_sha=member["reviewed_tree_sha"],
                    result="pending",
                    created_at=self.clock(),
                    updated_at=self.clock(),
                )
            )

    async def _applied(self, state, revision, member, head, *, parent_repair=None):
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await conn.execute(
                update(integration_candidate_member_results)
                .where(
                    integration_candidate_member_results.c.batch_id == state["batch"]["id"],
                    integration_candidate_member_results.c.revision == revision["revision"],
                    integration_candidate_member_results.c.member_ordinal == member["ordinal"],
                )
                .values(
                    result="applied",
                    generated_squash_sha=head,
                    conflict_evidence=parent_repair,
                    updated_at=self.clock(),
                )
            )
            await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == state["batch"]["id"],
                    integration_candidate_revisions.c.revision == revision["revision"],
                )
                .values(
                    next_member_ordinal=int(member["ordinal"]) + 1,
                    head_sha=head,
                    updated_at=self.clock(),
                )
            )
        return {**revision, "next_member_ordinal": int(member["ordinal"]) + 1, "head_sha": head}

    async def _accepted_parent_repair(self, revision, ordinal):
        parent = revision.get("repair_parent_revision")
        if parent is None:
            return None
        row = await self._member_result(revision["batch_id"], int(parent), ordinal)
        evidence = row.get("conflict_evidence") if row else None
        if row is None or row["result"] != "applied" or not evidence:
            return None
        accepted = evidence.get("accepted_lineage")
        if not accepted:
            return None
        return {"accepted_lineage": accepted}

    async def _conflict(self, state, revision, member, partial, evidence, operation_id):
        detail = {
            "batch_id": state["batch"]["id"],
            "revision": int(revision["revision"]),
            "ordinal": int(member["ordinal"]),
            "operation_id": operation_id,
            "operation_stage": 0,
            "partial_head_sha": partial,
            "source_base_sha": member["source_base_sha"],
            "source_head_sha": member["reviewed_head_sha"],
            "detail": str(evidence),
        }
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await conn.execute(
                update(integration_candidate_member_results)
                .where(
                    integration_candidate_member_results.c.batch_id == state["batch"]["id"],
                    integration_candidate_member_results.c.revision == revision["revision"],
                    integration_candidate_member_results.c.member_ordinal == member["ordinal"],
                )
                .values(result="conflict", conflict_evidence=detail, updated_at=self.clock())
            )
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == state["batch"]["id"])
                .values(lifecycle="repairing", updated_at=self.clock())
            )
        await self.repair.dispatch(operation_id, 0)
        return {
            **revision,
            "state": "conflict",
            "head_sha": partial,
            "member_ordinal": int(member["ordinal"]),
        }

    async def _valid_repair_lineage(self, store: Path, lineage: CandidateRepairLineage):
        if not lineage.repair_commit_shas:
            return False
        if not await self.git.ais_ancestor(
            str(store), lineage.partial_head_sha, lineage.resolved_head_sha, strict=True
        ):
            return False
        commits = await self.git.arun_git_result(
            [
                "rev-list",
                "--reverse",
                f"{lineage.partial_head_sha}..{lineage.resolved_head_sha}",
            ],
            cwd=str(store),
        )
        if commits.returncode != 0 or tuple(commits.stdout.split()) != lineage.repair_commit_shas:
            return False
        changed = await self.git.arun_git_result(
            ["diff", "--name-status", lineage.source_base_sha, lineage.source_head_sha],
            cwd=str(store),
        )
        if changed.returncode != 0 or not changed.stdout.strip():
            return False
        intended_paths = {line.split("\t", 1)[1] for line in changed.stdout.splitlines()}
        repaired = await self.git.arun_git_result(
            ["diff", "--name-only", lineage.partial_head_sha, lineage.resolved_head_sha],
            cwd=str(store),
        )
        if repaired.returncode != 0 or set(repaired.stdout.splitlines()) != intended_paths:
            return False
        merges = await self.git.arun_git_result(
            [
                "rev-list",
                "--min-parents=2",
                f"{lineage.partial_head_sha}..{lineage.resolved_head_sha}",
            ],
            cwd=str(store),
        )
        if merges.returncode != 0 or merges.stdout.strip():
            return False
        for line in changed.stdout.splitlines():
            status, path = line.split("\t", 1)
            source_blob = await self._blob(store, lineage.source_head_sha, path)
            resolved_blob = await self._blob(store, lineage.resolved_head_sha, path)
            partial_blob = await self._blob(store, lineage.partial_head_sha, path)
            if status.startswith("D"):
                if resolved_blob is not None:
                    return False
            elif resolved_blob is None or resolved_blob == partial_blob:
                return False
            elif status.startswith("A") and resolved_blob != source_blob:
                return False
        return True

    async def _blob(self, store: Path, commit: str, path: str) -> str | None:
        result = await self.git.arun_git_result(["rev-parse", f"{commit}:{path}"], cwd=str(store))
        return result.stdout.strip() if result.returncode == 0 else None

    async def _commit_exists(self, store: Path, oid: str) -> bool:
        result = await self.git.arun_git_result(
            ["cat-file", "-e", f"{oid}^{{commit}}"], cwd=str(store)
        )
        return result.returncode == 0

    async def _member_identity_matches(self, store: Path, member) -> bool:
        if not await self._commit_exists(store, member["source_base_sha"]):
            return False
        if not await self._commit_exists(store, member["reviewed_head_sha"]):
            return False
        tree = await self.git.arun_git_result(
            ["rev-parse", f"{member['reviewed_head_sha']}^{{tree}}"], cwd=str(store)
        )
        return tree.returncode == 0 and tree.stdout.strip() == member["reviewed_tree_sha"]

    @staticmethod
    def _repair_result(outcome, lineage):
        return CandidateRepairResult(
            outcome=outcome,
            batch_id=lineage.batch_id,
            revision=lineage.revision,
            member_ordinal=lineage.member_ordinal,
        )

    async def _member_result(self, batch_id, revision, ordinal):
        async with self.db._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_candidate_member_results).where(
                            integration_candidate_member_results.c.batch_id == batch_id,
                            integration_candidate_member_results.c.revision == revision,
                            integration_candidate_member_results.c.member_ordinal == ordinal,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None

    async def _revision(self, batch_id, revision):
        async with self.db._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_candidate_revisions).where(
                            integration_candidate_revisions.c.batch_id == batch_id,
                            integration_candidate_revisions.c.revision == revision,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None

    async def _repository(self, repository_id):
        value = (
            self.repository_resolver(repository_id)
            if self.repository_resolver
            else self.db.get_repo(repository_id)
        )
        if inspect.isawaitable(value):
            value = await value
        if value is None or value.id != repository_id or not value.url:
            raise ValueError("candidate repository is unavailable")
        return value

    async def _ensure_store(self, repository):
        digest = hashlib.sha256(repository.id.encode()).hexdigest()
        store = self.data_dir / "integration-repositories" / f"{digest}.git"
        store.parent.mkdir(parents=True, exist_ok=True)
        if not store.exists():
            result = await self.git.arun_git_result(
                ["clone", "--mirror", "--", repository.url, str(store)], cwd=str(store.parent)
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr or "candidate retained clone failed")
        return store

    async def _fetch(self, store, origin):
        result = await self.git.arun_git_result(
            ["fetch", "--prune", origin, "+refs/heads/*:refs/heads/*"], cwd=str(store)
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "candidate source fetch failed")

    async def _pin(self, store, ref, head):
        result = await self.git.arun_git_result(["update-ref", ref, head], cwd=str(store))
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "candidate recovery pin failed")

    async def _retain_sources(self, store: Path, state) -> None:
        digest = hashlib.sha256(state["batch"]["id"].encode()).hexdigest()
        for member in state["members"]:
            ordinal = int(member["ordinal"])
            for label, oid in (
                ("base", member["source_base_sha"]),
                ("head", member["reviewed_head_sha"]),
            ):
                if await self._commit_exists(store, oid):
                    await self._pin(
                        store,
                        f"refs/aq/integration-sources/{digest}/{ordinal}/{label}",
                        oid,
                    )

    async def _crash(self, point):
        if self.crash_hook:
            value = self.crash_hook(point)
            if inspect.isawaitable(value):
                await value

    @staticmethod
    def _recovery_ref(batch_id, revision):
        digest = hashlib.sha256(batch_id.encode()).hexdigest()
        return f"refs/aq/integration-candidates/{digest}/{revision}"

    async def _authors(self, store: Path, base: str, head: str):
        result = await self.git.arun_git_result(
            ["log", "--format=%an%x00%ae%x00%B%x00%x1e", f"{base}..{head}"],
            cwd=str(store),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "candidate author scan failed")
        identities: set[tuple[str, str]] = set()
        for record in result.stdout.split("\x1e"):
            fields = record.strip("\n\x00").split("\x00", 2)
            if len(fields) != 3:
                continue
            identities.add((fields[0].strip(), fields[1].strip().lower()))
            for match in _COAUTHOR_RE.finditer(fields[2]):
                identities.add((match.group("name").strip(), match.group("email").strip().lower()))
        return [
            {"name": name, "email": email}
            for email, name in sorted((email, name) for name, email in identities)
            if name and "@" in email and "\n" not in email
        ]

    @staticmethod
    def _message(state, member, authors, parent_repair=None):
        trailers = "".join(
            f"\nCo-authored-by: {author['name']} <{author['email']}>" for author in authors[1:]
        )
        repair = ""
        if parent_repair:
            commits = ",".join(parent_repair["accepted_lineage"]["repair_commit_shas"])
            repair = f"\nAccepted-repair-commits: {commits}"
        return (
            f"Integrate {member['task_id']}\n\nBatch: {state['batch']['id']}\n"
            f"Reviewed-head: {member['reviewed_head_sha']}\n"
            f"Review-evidence: {member['review_evidence_id']}{repair}{trailers}"
        )

    @staticmethod
    def _result(outcome, state, revision, operation_id):
        return CandidateBuildResult(
            outcome=outcome,
            batch_id=state["batch"]["id"],
            revision=int(revision["revision"]),
            operation_id=operation_id,
            head_sha=revision.get("head_sha"),
            branch=state["batch"]["integration_branch"],
            pr_url=revision.get("pr_url") or state["batch"].get("pr_url"),
            member_ordinal=revision.get("member_ordinal"),
        )
