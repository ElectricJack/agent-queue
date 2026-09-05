"""Ordered, restartable construction of sealed root integration candidates."""

from __future__ import annotations

import hashlib
import inspect
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from src.commands.principal import PrincipalKind, current_principal, matches_session_instance
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_member_results,
    integration_candidate_revisions,
    integration_candidate_publications,
    integration_candidate_ref_mutations,
    integration_candidate_resolutions,
    integration_repair_operations,
    project_integration_leases,
    projects,
)
from src.git.manager import GitManager, is_valid_git_oid
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
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
        "configuration_blocked",
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
    repository_numeric_id: int
    repository_full_name: str
    idempotency_key: str


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


class CandidateAuthorizationError(ValueError):
    """Raised when caller-supplied data attempts to stand in for durable authority."""


class CandidateStaleAuthority(RuntimeError):
    """The snapshotted hierarchy, lease, revision, or branch fence changed."""


class CandidateRepairResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    outcome: Literal["accepted", "already_accepted", "stale", "wait"]
    batch_id: str
    revision: int
    member_ordinal: int


class CandidateResolutionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    batch_id: str
    revision: int
    member_ordinal: int
    operation_id: str
    resolved_head_sha: str
    resolved_tree_sha: str
    repair_commit_shas: tuple[str, ...]
    fence: Fence


class AuditForgeProvider(Protocol):
    async def lookup_audit_pr(self, *, idempotency_key: str) -> AuditPullRequest | None: ...

    async def create_audit_pr(
        self,
        *,
        repository_id: str,
        branch: str,
        head_sha: str,
        base_branch: str,
        batch_id: str,
        idempotency_key: str,
        repository_numeric_id: int,
        repository_full_name: str,
    ) -> AuditPullRequest: ...


CrashHook = Callable[[str], Awaitable[None] | None]
RepositoryResolver = Callable[[str], Awaitable[Any] | Any]
_COAUTHOR_RE = re.compile(r"(?im)^co-authored-by:\s*(?P<name>[^<\n]+?)\s*<(?P<email>[^>\n]+)>\s*$")
_MUTATION_CLAIM_SECONDS = 60.0


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
        branch_ownership: BranchOwnership | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager or GitManager()
        self.repository_resolver = repository_resolver
        self.forge_provider = forge_provider
        self.app_client = app_client
        self.crash_hook = crash_hook
        self.repair = repair_service or RepairService(db, clock=clock)
        self.ownership = branch_ownership or BranchOwnership(db, clock=clock)
        self.clock = clock

    async def build(self, batch_id: str) -> CandidateBuildResult:
        state = await self._locked_state(batch_id)
        batch = state["batch"]
        if batch["lifecycle"] == "empty":
            return CandidateBuildResult(
                outcome="empty", batch_id=batch_id, revision=int(batch["current_revision"])
            )
        if state.get("authority_wait"):
            return CandidateBuildResult(
                outcome="wait", batch_id=batch_id, revision=int(batch["current_revision"])
            )
        if self.app_client is None or self.forge_provider is None:
            return CandidateBuildResult(
                outcome="configuration_blocked",
                batch_id=batch_id,
                revision=int(batch["current_revision"]),
                operation_id=state["operation"]["id"] if state["operation"] else None,
            )
        revision_number = int(batch["current_revision"])
        try:
            revision = await self._ensure_revision(state, revision_number, batch["base_sha"])
        except CandidateStaleAuthority:
            return CandidateBuildResult(outcome="wait", batch_id=batch_id, revision=revision_number)
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
        self._assert_repository_binding(repository)
        store = await self._ensure_store(repository)
        await self._fetch_inputs(store, state)
        await self._retain_sources(store, state)
        if revision["state"] != "built":
            try:
                revision = await self._construct(state, revision, store, operation_id=operation_id)
            except (CandidateStaleAuthority, StaleFence, BranchBusy):
                return CandidateBuildResult(
                    outcome="wait",
                    batch_id=batch_id,
                    revision=revision_number,
                    operation_id=operation_id,
                )
        if revision["state"] in {"conflict", "source_moved", "base_moved"}:
            return self._result(revision["state"], state, revision, operation_id)
        outcome = "already_built" if was_built or batch["pr_url"] else "built"
        pushed = await self._publish(state, revision, store)
        if pushed.get("publication_wait"):
            return self._result("wait", state, pushed, operation_id)
        return self._result(outcome, state, pushed, operation_id)

    async def rebuild(
        self, batch_id: str, expected_revision: int, new_base_sha: str
    ) -> CandidateBuildResult:
        if not is_valid_git_oid(new_base_sha):
            raise ValueError("candidate rebuild base must be an exact Git OID")
        state = await self._locked_state(batch_id)
        batch = state["batch"]
        if state.get("authority_wait"):
            return CandidateBuildResult(
                outcome="wait", batch_id=batch_id, revision=int(batch["current_revision"])
            )
        if self.app_client is None or self.forge_provider is None:
            return CandidateBuildResult(
                outcome="configuration_blocked",
                batch_id=batch_id,
                revision=int(batch["current_revision"]),
                operation_id=state["operation"]["id"],
            )
        if int(batch["current_revision"]) != expected_revision:
            return CandidateBuildResult(
                outcome="stale_revision",
                batch_id=batch_id,
                revision=int(batch["current_revision"]),
                operation_id=state["operation"]["id"],
            )
        if await self._has_live_mutation(batch_id, expected_revision):
            return CandidateBuildResult(
                outcome="wait",
                batch_id=batch_id,
                revision=expected_revision,
                operation_id=state["operation"]["id"],
            )
        repository = await self._repository(batch["repository_id"])
        self._assert_repository_binding(repository)
        store = await self._ensure_store(repository)
        authoritative_base = await self.app_client.exact_head_ref(repository.default_branch)
        if authoritative_base is None or authoritative_base != new_base_sha:
            return CandidateBuildResult(
                outcome="base_moved",
                batch_id=batch_id,
                revision=expected_revision,
                operation_id=state["operation"]["id"],
            )
        await self._fetch_oid(
            store,
            authoritative_base,
            f"refs/aq/integration-bases/{hashlib.sha256(batch_id.encode()).hexdigest()}",
        )
        if not await self._commit_exists(store, authoritative_base):
            return CandidateBuildResult(
                outcome="base_moved",
                batch_id=batch_id,
                revision=expected_revision,
                operation_id=state["operation"]["id"],
            )
        current = await self._revision(batch_id, expected_revision)
        if current is None or not current.get("head_sha"):
            raise ValueError("current candidate revision is not recoverable")
        await self._pin(store, self._recovery_ref(batch_id, expected_revision), current["head_sha"])
        await self._crash("after_superseded_pin")
        if await self.app_client.exact_head_ref(repository.default_branch) != authoritative_base:
            return CandidateBuildResult(
                outcome="base_moved",
                batch_id=batch_id,
                revision=expected_revision,
                operation_id=state["operation"]["id"],
            )
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch["project_id"])
            await self._validate_authority_on(conn, state, revision=expected_revision)
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

    async def reserve_repair(self, request: CandidateResolutionInput) -> str:
        """Freeze an exact candidate repair from the current instance-bound writer."""
        principal = current_principal()
        if (
            principal is None
            or principal.kind is not PrincipalKind.SESSION
            or principal.task_id is None
            or principal.session_id is None
            or principal.session_instance_token is None
        ):
            raise CandidateAuthorizationError("candidate repair requires a current session writer")
        for oid in (
            request.resolved_head_sha,
            request.resolved_tree_sha,
            *request.repair_commit_shas,
        ):
            if not is_valid_git_oid(oid):
                raise ValueError("candidate repair contains a non-OID")
        reservation_id = str(
            uuid.uuid5(
                uuid.UUID("afe86ae2-2723-4c36-9933-91e4dc4cae7a"),
                f"{request.batch_id}:{request.revision}:{request.member_ordinal}",
            )
        )
        target_branch = f"refs/heads/aq/integration-repairs/{reservation_id}"
        async with self.db._engine.connect() as read_conn:
            project_id = (
                await read_conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == request.batch_id
                    )
                )
            ).scalar_one_or_none()
        if project_id is None:
            raise CandidateAuthorizationError("candidate repair batch is absent")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                await conn.execute(
                    select(projects).where(projects.c.id == project_id).with_for_update()
                )
            ).mappings().one()
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == request.batch_id)
                    .with_for_update()
                )
            ).mappings().one()
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == request.operation_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            lease = (
                await conn.execute(
                    select(project_integration_leases)
                    .where(project_integration_leases.c.project_id == project_id)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            evidence = (
                await conn.execute(
                    select(integration_candidate_member_results).where(
                        integration_candidate_member_results.c.batch_id == request.batch_id,
                        integration_candidate_member_results.c.revision == request.revision,
                        integration_candidate_member_results.c.member_ordinal
                        == request.member_ordinal,
                    )
                )
            ).mappings().one_or_none()
            detail = evidence["conflict_evidence"] if evidence else None
            scope = await self.db.get_repair_filing_scope(
                principal.task_id, session_id=principal.session_id, conn=conn
            )
            if (
                int(batch["current_revision"]) != request.revision
                or batch["project_id"] != project_id
                or project["hierarchical_integration_mode"] != "train"
                or project["integration_repository_id"] != batch["repository_id"]
                or lease is None
                or lease["batch_id"] != request.batch_id
                or lease["repository_id"] != batch["repository_id"]
                or float(lease["expires_at"]) <= self.clock()
                or request.fence.target.repository_id != batch["repository_id"]
                or request.fence.target.branch != batch["integration_branch"]
                or evidence is None
                or evidence["result"] != "conflict"
                or not detail
                or operation is None
                or operation["batch_id"] != request.batch_id
                or operation["state"] not in {"active", "escalated"}
                or int(operation["active_stage"]) != int(scope["stage"] if scope else -1)
                or scope is None
                or not scope["active"]
                or scope["target_kind"] != "batch"
                or scope["operation_id"] != request.operation_id
                or scope["repository_id"] != batch["repository_id"]
                or scope["writer_kind"] != "repair_delegate"
                or scope["workspace_id"] is None
                or not scope["instance_token"]
                or not matches_session_instance(principal, scope["instance_token"])
                or request.fence.owner_id != principal.task_id
                or request.fence.token != scope["fence_token"]
                or self.clock() >= float(scope["deadline_at"])
            ):
                raise CandidateAuthorizationError("candidate repair writer authority is stale")
            async with self.ownership.mutation_exclusion_on(
                conn, request.fence, state="attached", expected_role="repair"
            ):
                existing = (
                    (
                        await conn.execute(
                            select(integration_candidate_resolutions).where(
                                integration_candidate_resolutions.c.id == reservation_id
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    await conn.execute(
                        insert(integration_candidate_resolutions).values(
                            id=reservation_id,
                            batch_id=request.batch_id,
                            revision=request.revision,
                            member_ordinal=request.member_ordinal,
                            operation_id=request.operation_id,
                            operation_episode_id=operation["episode_id"],
                            stage_ordinal=scope["stage"],
                            stage_deadline_at=scope["deadline_at"],
                            project_id=project_id,
                            repair_task_id=principal.task_id,
                            repair_session_id=principal.session_id,
                            repair_session_instance_token=scope["instance_token"],
                            repair_workspace_id=scope["workspace_id"],
                            repository_id=batch["repository_id"],
                            branch=request.fence.target.branch,
                            target_branch=target_branch,
                            fence_owner_id=request.fence.owner_id,
                            fence_token=request.fence.token,
                            partial_head_sha=detail["partial_head_sha"],
                            source_base_sha=detail["source_base_sha"],
                            source_head_sha=detail["source_head_sha"],
                            resolved_head_sha=request.resolved_head_sha,
                            resolved_tree_sha=request.resolved_tree_sha,
                            repair_commit_shas=list(request.repair_commit_shas),
                            state="reserved",
                            created_at=self.clock(),
                            updated_at=self.clock(),
                        )
                    )
                elif any(
                    existing[key] != value
                    for key, value in {
                        "operation_id": request.operation_id,
                        "operation_episode_id": operation["episode_id"],
                        "stage_ordinal": scope["stage"],
                        "stage_deadline_at": scope["deadline_at"],
                        "project_id": project_id,
                        "resolved_head_sha": request.resolved_head_sha,
                        "resolved_tree_sha": request.resolved_tree_sha,
                        "repair_commit_shas": list(request.repair_commit_shas),
                        "fence_owner_id": request.fence.owner_id,
                        "fence_token": request.fence.token,
                        "repair_task_id": principal.task_id,
                        "repair_session_id": principal.session_id,
                        "repair_session_instance_token": scope["instance_token"],
                        "repair_workspace_id": scope["workspace_id"],
                        "repository_id": batch["repository_id"],
                        "branch": request.fence.target.branch,
                        "target_branch": target_branch,
                    }.items()
                ):
                    raise CandidateAuthorizationError(
                        "candidate repair reservation identity differs"
                    )
        return reservation_id

    async def accept_repair(self, lineage: CandidateRepairLineage | str) -> CandidateRepairResult:
        if isinstance(lineage, CandidateRepairLineage):
            raise CandidateAuthorizationError(
                "caller repair lineage is not authority; a pushed server reservation is required"
            )
        reservation = await self._resolution(lineage)
        if reservation is None:
            raise CandidateAuthorizationError("candidate repair reservation does not exist")
        if reservation["state"] == "accepted":
            return self._repair_result("already_accepted", reservation)
        if reservation["state"] != "pushed":
            raise CandidateAuthorizationError("candidate repair reservation has not been pushed")
        state = await self._locked_state(reservation["batch_id"])
        if int(state["batch"]["current_revision"]) != int(reservation["revision"]) or int(
            state["operation"]["active_stage"]
        ) != int(reservation["stage_ordinal"]):
            return self._repair_result("stale", reservation)
        branch = reservation["target_branch"].removeprefix("refs/heads/")
        if await self.app_client.exact_head_ref(branch) != reservation["resolved_head_sha"]:
            return self._repair_result("stale", reservation)
        repository = await self._repository(reservation["repository_id"])
        store = await self._ensure_store(repository)
        await self._fetch_oid(
            store,
            reservation["resolved_head_sha"],
            f"refs/aq/integration-resolutions/{lineage}",
        )
        repair_lineage = CandidateRepairLineage(
            batch_id=reservation["batch_id"],
            revision=reservation["revision"],
            member_ordinal=reservation["member_ordinal"],
            operation_id=reservation["operation_id"],
            operation_stage=reservation["stage_ordinal"],
            partial_head_sha=reservation["partial_head_sha"],
            source_base_sha=reservation["source_base_sha"],
            source_head_sha=reservation["source_head_sha"],
            resolved_head_sha=reservation["resolved_head_sha"],
            repair_commit_shas=tuple(reservation["repair_commit_shas"]),
        )
        if not await self._valid_repair_lineage(store, repair_lineage):
            return self._repair_result("stale", reservation)
        resolved_tree = await self.git.arun_git_result(
            ["rev-parse", f"{reservation['resolved_head_sha']}^{{tree}}"], cwd=str(store)
        )
        if (
            resolved_tree.returncode != 0
            or resolved_tree.stdout.strip() != reservation["resolved_tree_sha"]
        ):
            return self._repair_result("stale", reservation)
        if await self.git.areserved_paths_in_diff(
            str(store), reservation["source_base_sha"], reservation["source_head_sha"]
        ) or await self.git.areserved_paths_in_diff(
            str(store), reservation["partial_head_sha"], reservation["resolved_head_sha"]
        ):
            return self._repair_result("stale", reservation)
        repair_fence = Fence(
            target=BranchKey(
                repository_id=reservation["repository_id"], branch=reservation["branch"]
            ),
            owner_id=reservation["fence_owner_id"],
            token=reservation["fence_token"],
        )
        try:
            collector = await self.ownership.transfer(
                repair_fence, reservation["operation_id"], "collector"
            )
        except BranchBusy:
            return self._repair_result("wait", reservation)
        state["fence"] = collector
        token = await self.app_client.installation_token()
        handed_back = await self._mutate_ref(
            state,
            revision=int(reservation["revision"]),
            purpose="repair_handoff",
            target_branch=reservation["branch"],
            expected_old_sha=reservation["partial_head_sha"],
            desired_sha=reservation["resolved_head_sha"],
            store=store,
            token=token,
            member_ordinal=int(reservation["member_ordinal"]),
            resolution_id=lineage,
        )
        if not handed_back:
            return self._repair_result("wait", reservation)
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await self._validate_authority_on(conn, state, revision=reservation["revision"])
            member = await conn.execute(
                update(integration_candidate_member_results)
                .where(
                    integration_candidate_member_results.c.batch_id == reservation["batch_id"],
                    integration_candidate_member_results.c.revision == reservation["revision"],
                    integration_candidate_member_results.c.member_ordinal
                    == reservation["member_ordinal"],
                    integration_candidate_member_results.c.result == "conflict",
                )
                .values(
                    result="applied",
                    generated_squash_sha=reservation["resolved_head_sha"],
                    conflict_evidence={"accepted_reservation_id": lineage},
                    updated_at=now,
                )
            )
            cursor = await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == reservation["batch_id"],
                    integration_candidate_revisions.c.revision == reservation["revision"],
                    integration_candidate_revisions.c.next_member_ordinal
                    == reservation["member_ordinal"],
                )
                .values(
                    next_member_ordinal=reservation["member_ordinal"] + 1,
                    head_sha=reservation["resolved_head_sha"],
                    updated_at=now,
                )
            )
            consumed = await conn.execute(
                update(integration_candidate_resolutions)
                .where(
                    integration_candidate_resolutions.c.id == lineage,
                    integration_candidate_resolutions.c.state == "pushed",
                )
                .values(state="accepted", updated_at=now)
            )
            if member.rowcount != 1 or cursor.rowcount != 1 or consumed.rowcount != 1:
                raise CandidateStaleAuthority("candidate repair acceptance CAS lost")
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == reservation["batch_id"])
                .values(lifecycle="building", updated_at=now)
            )
        return self._repair_result("accepted", reservation)

    async def push_repair(self, reservation_id: str, fence: Fence) -> str:
        principal = current_principal()
        reservation = await self._resolution(reservation_id)
        if reservation is None or principal is None or principal.kind is not PrincipalKind.SESSION:
            raise CandidateAuthorizationError("candidate repair push authority is absent")
        if reservation["state"] in {"pushed", "accepted"}:
            return reservation_id
        state, scope = await self._repair_state(reservation, principal, fence)
        token = await self.app_client.installation_token()
        pushed = await self._mutate_ref(
            state,
            revision=int(reservation["revision"]),
            purpose="repair_resolution",
            target_branch=reservation["target_branch"],
            expected_old_sha="0" * 40,
            desired_sha=reservation["resolved_head_sha"],
            store=Path(scope["workspace_path"]),
            token=token,
            member_ordinal=int(reservation["member_ordinal"]),
            resolution_id=reservation_id,
            expected_role="repair",
            expected_handoff="attached",
        )
        if not pushed:
            raise CandidateStaleAuthority("candidate repair publication is waiting")
        await self._crash("after_repair_push")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await self._validate_authority_on(
                conn,
                state,
                revision=int(reservation["revision"]),
                expected_role="repair",
                expected_handoff="attached",
            )
            changed = await conn.execute(
                update(integration_candidate_resolutions)
                .where(
                    integration_candidate_resolutions.c.id == reservation_id,
                    integration_candidate_resolutions.c.state == "reserved",
                )
                .values(
                    state="pushed",
                    push_evidence={
                        "remote_sha": reservation["resolved_head_sha"],
                        "target_branch": reservation["target_branch"],
                    },
                    updated_at=self.clock(),
                )
            )
            if changed.rowcount != 1:
                canonical = await self._resolution_on(conn, reservation_id)
                if canonical is None or canonical["state"] not in {"pushed", "accepted"}:
                    raise CandidateStaleAuthority("candidate repair push CAS lost")
        return reservation_id

    async def _repair_state(self, reservation, principal, fence):
        async with self.db._engine.connect() as read_conn:
            project_id = (
                await read_conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == reservation["batch_id"]
                    )
                )
            ).scalar_one_or_none()
        if project_id is None:
            raise CandidateAuthorizationError("candidate repair batch is absent")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                await conn.execute(select(projects).where(projects.c.id == project_id))
            ).mappings().one()
            batch = (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == reservation["batch_id"])
                    .with_for_update()
                )
            ).mappings().one()
            lease = (
                await conn.execute(
                    select(project_integration_leases)
                    .where(project_integration_leases.c.project_id == project_id)
                    .with_for_update()
                )
            ).mappings().one()
            operation = (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == reservation["operation_id"])
                    .with_for_update()
                )
            ).mappings().one()
            scope = await self.db.get_repair_filing_scope(
                principal.task_id, session_id=principal.session_id, conn=conn
            )
            if (
                int(batch["current_revision"]) != int(reservation["revision"])
                or batch["repository_id"] != reservation["repository_id"]
                or batch["project_id"] != reservation["project_id"]
                or batch["integration_branch"] != reservation["branch"]
                or operation["batch_id"] != reservation["batch_id"]
                or operation["episode_id"] != reservation["operation_episode_id"]
                or int(operation["active_stage"]) != int(reservation["stage_ordinal"])
                or operation["state"] not in {"active", "escalated"}
                or scope is None
                or not scope["active"]
                or scope["operation_id"] != reservation["operation_id"]
                or int(scope["stage"]) != int(reservation["stage_ordinal"])
                or principal.task_id != reservation["repair_task_id"]
                or principal.session_id != reservation["repair_session_id"]
                or scope["instance_token"] != reservation["repair_session_instance_token"]
                or not matches_session_instance(
                    principal, reservation["repair_session_instance_token"]
                )
                or scope["workspace_id"] != reservation["repair_workspace_id"]
                or scope["repository_id"] != reservation["repository_id"]
                or scope["workspace_path"] is None
                or fence.target.repository_id != reservation["repository_id"]
                or fence.target.branch != reservation["branch"]
                or fence.owner_id != reservation["fence_owner_id"]
                or fence.token != int(reservation["fence_token"])
                or self.clock() >= float(scope["deadline_at"])
                or float(scope["deadline_at"]) != float(reservation["stage_deadline_at"])
            ):
                raise CandidateAuthorizationError("candidate repair push authority is stale")
            async with self.ownership.mutation_exclusion_on(
                conn, fence, state="attached", expected_role="repair"
            ):
                pass
        return {
            "project": dict(project),
            "batch": dict(batch),
            "lease": dict(lease),
            "operation": dict(operation),
            "fence": fence,
        }, dict(scope)

    async def _locked_state(self, batch_id: str) -> dict[str, Any]:
        # Resolve only the lock key before entering the canonical hierarchy-first
        # transaction.  No authority decision is made from this first read.
        async with self.db._engine.connect() as read_conn:
            project_id = (
                await read_conn.execute(
                    select(integration_batches.c.project_id).where(
                        integration_batches.c.id == batch_id
                    )
                )
            ).scalar_one_or_none()
        if project_id is None:
            raise ValueError("integration batch does not exist")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            project = (
                (
                    await conn.execute(
                        select(projects).where(projects.c.id == project_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if project is None:
                raise ValueError("integration batch project does not exist")
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
            if batch["project_id"] != project_id:
                raise ValueError("integration batch project identity changed")
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
                or float(lease["expires_at"]) <= self.clock()
            ):
                return {
                    "batch": dict(batch),
                    "project": dict(project),
                    "members": [dict(row) for row in members],
                    "operation": None,
                    "authority_wait": True,
                }
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
            fence = None
            if operation is not None:
                target = BranchKey(
                    repository_id=batch["repository_id"], branch=batch["integration_branch"]
                )
                try:
                    fence = await self.ownership.acquire(
                        target, operation["id"], "collector", conn=conn
                    )
                except BranchBusy:
                    return {
                        "batch": dict(batch),
                        "project": dict(project),
                        "members": [dict(row) for row in members],
                        "operation": dict(operation),
                        "lease": dict(lease),
                        "authority_wait": True,
                    }
            return {
                "batch": dict(batch),
                "project": dict(project),
                "members": [dict(row) for row in members],
                "operation": dict(operation) if operation else None,
                "lease": dict(lease) if lease else None,
                "fence": fence,
            }

    async def _ensure_revision(self, state, revision: int, base_sha: str) -> dict[str, Any]:
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await self._validate_authority_on(conn, state, revision=revision)
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
                changed = await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == state["batch"]["id"],
                        integration_batches.c.current_revision == revision,
                        integration_batches.c.lifecycle == state["batch"]["lifecycle"],
                    )
                    .values(lifecycle="building", updated_at=now)
                )
                if changed.rowcount != 1:
                    raise CandidateStaleAuthority("batch changed while reserving revision")
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
                return await self._conflict(
                    state,
                    revision,
                    member,
                    evidence["partial_head_sha"],
                    evidence.get("detail", "replay"),
                    operation_id,
                )
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
            parent_repair = await self._accepted_parent_repair(revision, ordinal, store)
            if parent_repair is None:
                merge_args = [
                    "merge-tree",
                    "--write-tree",
                    f"--merge-base={member['source_base_sha']}",
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
            await self._validate_authority_on(conn, state, revision=int(revision["revision"]))
            changed = await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == batch_id,
                    integration_candidate_revisions.c.revision == revision["revision"],
                    integration_candidate_revisions.c.next_member_ordinal == len(state["members"]),
                    integration_candidate_revisions.c.state == "constructing",
                )
                .values(state="built", head_sha=current, updated_at=self.clock())
            )
            if changed.rowcount != 1:
                raise CandidateStaleAuthority("candidate completion CAS lost")
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == batch_id)
                .values(lifecycle="testing", updated_at=self.clock())
            )
        return {**revision, "state": "built", "head_sha": current}

    async def _publish(self, state, revision, store):
        batch = state["batch"]
        branch = batch["integration_branch"].removeprefix("refs/heads/")
        binding = self.app_client.repository
        repository = await self._repository(batch["repository_id"])
        publication_key = hashlib.sha256(
            f"{batch['id']}:{revision['revision']}:{revision['head_sha']}".encode()
        ).hexdigest()
        previous = await self._revision(batch["id"], int(revision["revision"]) - 1)
        expected_old = previous.get("head_sha") if previous else "0" * 40
        now = self.clock()
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch["project_id"])
            await self._validate_authority_on(conn, state, revision=int(revision["revision"]))
            row = (
                (
                    await conn.execute(
                        select(integration_candidate_publications).where(
                            integration_candidate_publications.c.batch_id == batch["id"],
                            integration_candidate_publications.c.revision == revision["revision"],
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                await conn.execute(
                    insert(integration_candidate_publications).values(
                        batch_id=batch["id"],
                        revision=revision["revision"],
                        state="reserved",
                        repository_id=repository.id,
                        repository_numeric_id=binding.repository_id,
                        repository_full_name=binding.full_name,
                        base_ref=repository.default_branch,
                        head_ref=branch,
                        head_sha=revision["head_sha"],
                        expected_old_sha=expected_old,
                        idempotency_key=publication_key,
                        created_at=now,
                        updated_at=now,
                    )
                )
                row = {"state": "reserved", "expected_old_sha": expected_old}
        token = await self.app_client.installation_token()
        published = await self._mutate_ref(
            state,
            revision=int(revision["revision"]),
            purpose="candidate_final",
            target_branch=batch["integration_branch"],
            expected_old_sha=row["expected_old_sha"],
            desired_sha=revision["head_sha"],
            store=store,
            token=token,
        )
        if not published:
            return {**revision, "publication_wait": True}
        await self._crash("after_candidate_push")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch["project_id"])
            await self._validate_authority_on(conn, state, revision=int(revision["revision"]))
            changed = await conn.execute(
                update(integration_candidate_publications)
                .where(
                    integration_candidate_publications.c.batch_id == batch["id"],
                    integration_candidate_publications.c.revision == revision["revision"],
                    integration_candidate_publications.c.state == "reserved",
                )
                .values(state="ref_published", updated_at=self.clock())
            )
            if changed.rowcount == 0:
                canonical = (
                    await conn.execute(
                        select(integration_candidate_publications).where(
                            integration_candidate_publications.c.batch_id == batch["id"],
                            integration_candidate_publications.c.revision == revision["revision"],
                        )
                    )
                ).mappings().one()
                if canonical["state"] not in {"ref_published", "pr_reserved", "pr_published"}:
                    raise CandidateStaleAuthority("candidate ref publication CAS lost")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch["project_id"])
            await self._validate_authority_on(conn, state, revision=int(revision["revision"]))
            await conn.execute(
                update(integration_candidate_publications)
                .where(
                    integration_candidate_publications.c.batch_id == batch["id"],
                    integration_candidate_publications.c.revision == revision["revision"],
                    integration_candidate_publications.c.state == "ref_published",
                )
                .values(state="pr_reserved", updated_at=self.clock())
            )
        pr = await self.forge_provider.lookup_audit_pr(idempotency_key=publication_key)
        if pr is None:
            pr = await self.forge_provider.create_audit_pr(
                repository_id=batch["repository_id"],
                branch=branch,
                head_sha=revision["head_sha"],
                base_branch=repository.default_branch,
                batch_id=batch["id"],
                idempotency_key=publication_key,
                repository_numeric_id=binding.repository_id,
                repository_full_name=binding.full_name,
            )
        await self._crash("after_audit_pr_create")
        expected_url = (
            f"https://github.com/{binding.full_name}/pull/{pr.number}"
        )
        if (
            pr.number <= 0
            or pr.url != expected_url
            or pr.head_sha != revision["head_sha"]
            or pr.head_branch != branch
            or pr.base_branch != repository.default_branch
            or pr.repository_numeric_id != binding.repository_id
            or pr.repository_full_name != binding.full_name
            or pr.idempotency_key != publication_key
        ):
            raise ValueError("audit PR identity does not match candidate")
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch["project_id"])
            await self._validate_authority_on(conn, state, revision=int(revision["revision"]))
            changed = await conn.execute(
                update(integration_candidate_publications)
                .where(
                    integration_candidate_publications.c.batch_id == batch["id"],
                    integration_candidate_publications.c.revision == revision["revision"],
                    integration_candidate_publications.c.state == "pr_reserved",
                )
                .values(
                    state="pr_published",
                    pr_number=pr.number,
                    pr_url=pr.url,
                    updated_at=self.clock(),
                )
            )
            if changed.rowcount == 0:
                canonical = (
                    await conn.execute(
                        select(integration_candidate_publications).where(
                            integration_candidate_publications.c.batch_id == batch["id"],
                            integration_candidate_publications.c.revision == revision["revision"],
                        )
                    )
                ).mappings().one()
                if (
                    canonical["state"] != "pr_published"
                    or canonical["pr_number"] != pr.number
                    or canonical["pr_url"] != pr.url
                ):
                    raise CandidateStaleAuthority("publication canonical PR differs")
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == batch["id"])
                .values(pr_url=pr.url, updated_at=self.clock())
            )
        await self._crash("after_audit_pr_write")
        return {**revision, "pr_url": pr.url}

    async def _pending(self, state, revision, member):
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await self._validate_authority_on(conn, state, revision=int(revision["revision"]))
            existing = (
                await conn.execute(
                    select(integration_candidate_member_results).where(
                        integration_candidate_member_results.c.batch_id == state["batch"]["id"],
                        integration_candidate_member_results.c.revision == revision["revision"],
                        integration_candidate_member_results.c.member_ordinal == member["ordinal"],
                    )
                )
            ).mappings().one_or_none()
            if existing is not None:
                return
            try:
                async with conn.begin_nested():
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
            except IntegrityError:
                canonical = (
                    await conn.execute(
                        select(integration_candidate_member_results).where(
                            integration_candidate_member_results.c.batch_id
                            == state["batch"]["id"],
                            integration_candidate_member_results.c.revision
                            == revision["revision"],
                            integration_candidate_member_results.c.member_ordinal
                            == member["ordinal"],
                        )
                    )
                ).mappings().one_or_none()
                if canonical is None:
                    raise CandidateStaleAuthority("candidate pending insert raced") from None

    async def _applied(self, state, revision, member, head, *, parent_repair=None):
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await self._validate_authority_on(conn, state, revision=int(revision["revision"]))
            member_changed = await conn.execute(
                update(integration_candidate_member_results)
                .where(
                    integration_candidate_member_results.c.batch_id == state["batch"]["id"],
                    integration_candidate_member_results.c.revision == revision["revision"],
                    integration_candidate_member_results.c.member_ordinal == member["ordinal"],
                    integration_candidate_member_results.c.result == "pending",
                )
                .values(
                    result="applied",
                    generated_squash_sha=head,
                    conflict_evidence=parent_repair,
                    updated_at=self.clock(),
                )
            )
            revision_changed = await conn.execute(
                update(integration_candidate_revisions)
                .where(
                    integration_candidate_revisions.c.batch_id == state["batch"]["id"],
                    integration_candidate_revisions.c.revision == revision["revision"],
                    integration_candidate_revisions.c.next_member_ordinal == member["ordinal"],
                    integration_candidate_revisions.c.state == "constructing",
                )
                .values(
                    next_member_ordinal=int(member["ordinal"]) + 1,
                    head_sha=head,
                    updated_at=self.clock(),
                )
            )
            if member_changed.rowcount != 1 or revision_changed.rowcount != 1:
                raise CandidateStaleAuthority("candidate progress CAS lost")
        return {**revision, "next_member_ordinal": int(member["ordinal"]) + 1, "head_sha": head}

    async def _validate_authority_on(
        self,
        conn,
        state,
        *,
        revision: int,
        expected_role: str = "collector",
        expected_handoff: str = "reserved",
    ) -> None:
        project = (
            (
                await conn.execute(
                    select(projects)
                    .where(projects.c.id == state["project"]["id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        batch = (
            (
                await conn.execute(
                    select(integration_batches)
                    .where(integration_batches.c.id == state["batch"]["id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        lease = (
            (
                await conn.execute(
                    select(project_integration_leases)
                    .where(project_integration_leases.c.project_id == state["project"]["id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        operation = (
            (
                await conn.execute(
                    select(integration_repair_operations)
                    .where(integration_repair_operations.c.id == state["operation"]["id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            project is None
            or project["hierarchical_integration_mode"] != "train"
            or project["integration_repository_id"] != state["batch"]["repository_id"]
            or batch is None
            or int(batch["current_revision"]) != revision
            or batch["repository_id"] != state["batch"]["repository_id"]
            or lease is None
            or lease["batch_id"] != batch["id"]
            or lease["owner_id"] != state["lease"]["owner_id"]
            or int(lease["fence_token"]) != int(state["lease"]["fence_token"])
            or float(lease["expires_at"]) <= self.clock()
            or operation is None
            or operation["id"] != state["operation"]["id"]
            or operation["episode_id"] != state["operation"]["episode_id"]
            or int(operation["active_stage"]) != int(state["operation"]["active_stage"])
            or operation["state"] not in {"active", "escalated"}
        ):
            raise CandidateStaleAuthority("candidate authority changed")
        owner = (
            (
                await conn.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.repository_id
                        == state["fence"].target.repository_id,
                        integration_branch_owners.c.ref == state["fence"].target.branch,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            owner is None
            or owner["owner_id"] != state["fence"].owner_id
            or int(owner["fence_token"]) != state["fence"].token
            or owner["owner_role"] != expected_role
            or owner["handoff_state"] != expected_handoff
        ):
            raise CandidateStaleAuthority("candidate branch fence changed")

    @staticmethod
    def _mutation_id(*, purpose: str, batch_id: str, revision: int, ordinal, resolution_id) -> str:
        identity = f"{purpose}:{batch_id}:{revision}:{ordinal}:{resolution_id or ''}"
        return str(uuid.uuid5(uuid.UUID("fa976640-ae31-47b5-86c4-5fc250818fdd"), identity))

    async def _mutate_ref(
        self,
        state,
        *,
        revision: int,
        purpose: str,
        target_branch: str,
        expected_old_sha: str,
        desired_sha: str,
        store: Path,
        token: str,
        member_ordinal: int | None = None,
        resolution_id: str | None = None,
        expected_role: str = "collector",
        expected_handoff: str = "reserved",
    ) -> bool:
        """Reserve, perform, and reconcile one exact authenticated ref mutation.

        The database transactions only establish/recheck authority.  Authenticated
        ref reads and pushes happen after commit.  A different live nonce waits;
        an abandoned nonce is recoverable only after inspecting the remote tip.
        """
        mutation_id = self._mutation_id(
            purpose=purpose,
            batch_id=state["batch"]["id"],
            revision=revision,
            ordinal=member_ordinal,
            resolution_id=resolution_id,
        )
        nonce = str(uuid.uuid4())
        now = self.clock()
        owns = False
        already_applied = False
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await self._validate_authority_on(
                conn,
                state,
                revision=revision,
                expected_role=expected_role,
                expected_handoff=expected_handoff,
            )
            row = (
                (
                    await conn.execute(
                        select(integration_candidate_ref_mutations)
                        .where(integration_candidate_ref_mutations.c.id == mutation_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            identity = {
                "batch_id": state["batch"]["id"],
                "revision": revision,
                "member_ordinal": member_ordinal,
                "resolution_id": resolution_id,
                "purpose": purpose,
                "repository_id": state["batch"]["repository_id"],
                "branch": state["fence"].target.branch,
                "target_branch": target_branch,
                "expected_old_sha": expected_old_sha,
                "desired_sha": desired_sha,
                "operation_id": state["operation"]["id"],
                "operation_episode_id": state["operation"]["episode_id"],
                "operation_stage": int(state["operation"]["active_stage"]),
                "lease_owner_id": state["lease"]["owner_id"],
                "lease_fence_token": int(state["lease"]["fence_token"]),
                "branch_owner_id": state["fence"].owner_id,
                "branch_owner_role": expected_role,
                "branch_fence_token": state["fence"].token,
            }
            if row is None:
                await conn.execute(
                    insert(integration_candidate_ref_mutations).values(
                        id=mutation_id,
                        **identity,
                        nonce=nonce,
                        state="reserved",
                        expires_at=min(
                            now + _MUTATION_CLAIM_SECONDS,
                            float(state["lease"]["expires_at"]),
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                )
                owns = True
                row = {
                    **identity,
                    "nonce": nonce,
                    "state": "reserved",
                    "expires_at": min(
                        now + _MUTATION_CLAIM_SECONDS,
                        float(state["lease"]["expires_at"]),
                    ),
                }
            elif any(row[key] != value for key, value in identity.items()):
                raise CandidateStaleAuthority("candidate mutation identity changed")
            elif row["state"] == "applied":
                already_applied = True

        remote = await self.app_client.exact_head_ref(target_branch.removeprefix("refs/heads/"))
        if already_applied:
            return remote == desired_sha
        if owns and float(row["expires_at"]) <= self.clock():
            owns = False
        if remote == desired_sha:
            pass
        elif remote != expected_old_sha and not (remote is None and expected_old_sha == "0" * 40):
            return False
        elif not owns:
            if float(row["expires_at"]) > self.clock():
                return False
            takeover_nonce = str(uuid.uuid4())
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, state["project"]["id"])
                await self._validate_authority_on(
                    conn,
                    state,
                    revision=revision,
                    expected_role=expected_role,
                    expected_handoff=expected_handoff,
                )
                changed = await conn.execute(
                    update(integration_candidate_ref_mutations)
                    .where(
                        integration_candidate_ref_mutations.c.id == mutation_id,
                        integration_candidate_ref_mutations.c.state == "reserved",
                        integration_candidate_ref_mutations.c.nonce == row["nonce"],
                        integration_candidate_ref_mutations.c.expires_at <= self.clock(),
                    )
                    .values(
                        nonce=takeover_nonce,
                        expires_at=min(
                            self.clock() + _MUTATION_CLAIM_SECONDS,
                            float(state["lease"]["expires_at"]),
                        ),
                        updated_at=self.clock(),
                    )
                )
                if changed.rowcount != 1:
                    return False
            nonce = takeover_nonce
            owns = True
        if remote != desired_sha:
            if not owns:
                return False
            await self.git.apush_oid_with_app_auth(
                str(store),
                repository=self.app_client.repository,
                token=token,
                tip_oid=desired_sha,
                branch=target_branch.removeprefix("refs/heads/"),
                expected_old_oid=expected_old_sha,
            )

        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await self._validate_authority_on(
                conn,
                state,
                revision=revision,
                expected_role=expected_role,
                expected_handoff=expected_handoff,
            )
            changed = await conn.execute(
                update(integration_candidate_ref_mutations)
                .where(
                    integration_candidate_ref_mutations.c.id == mutation_id,
                    integration_candidate_ref_mutations.c.state == "reserved",
                    integration_candidate_ref_mutations.c.desired_sha == desired_sha,
                    integration_candidate_ref_mutations.c.nonce == row["nonce"]
                    if not owns
                    else integration_candidate_ref_mutations.c.nonce == nonce,
                )
                .values(state="applied", remote_sha=desired_sha, updated_at=self.clock())
            )
            if changed.rowcount == 0:
                canonical = (
                    (
                        await conn.execute(
                            select(integration_candidate_ref_mutations).where(
                                integration_candidate_ref_mutations.c.id == mutation_id
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if canonical["state"] != "applied" or canonical["remote_sha"] != desired_sha:
                    raise CandidateStaleAuthority("candidate mutation reconciliation CAS lost")
        return True

    async def _accepted_parent_repair(self, revision, ordinal, store: Path):
        parent = revision.get("repair_parent_revision")
        if parent is None:
            return None
        row = await self._member_result(revision["batch_id"], int(parent), ordinal)
        evidence = row.get("conflict_evidence") if row else None
        if row is None or row["result"] != "applied" or not evidence:
            return None
        reservation_id = evidence.get("accepted_reservation_id")
        if not reservation_id:
            return None
        accepted = await self._resolution(reservation_id)
        if (
            accepted is None
            or accepted["state"] != "accepted"
            or accepted["batch_id"] != revision["batch_id"]
            or int(accepted["revision"]) != int(parent)
            or int(accepted["member_ordinal"]) != int(ordinal)
        ):
            raise CandidateStaleAuthority("accepted candidate repair identity changed")
        await self._fetch_oid(
            store,
            accepted["resolved_head_sha"],
            f"refs/aq/integration-resolutions/{reservation_id}",
        )
        lineage = CandidateRepairLineage(
            batch_id=accepted["batch_id"],
            revision=int(accepted["revision"]),
            member_ordinal=int(accepted["member_ordinal"]),
            operation_id=accepted["operation_id"],
            operation_stage=int(accepted["stage_ordinal"]),
            partial_head_sha=accepted["partial_head_sha"],
            source_base_sha=accepted["source_base_sha"],
            source_head_sha=accepted["source_head_sha"],
            resolved_head_sha=accepted["resolved_head_sha"],
            repair_commit_shas=tuple(accepted["repair_commit_shas"]),
        )
        if not await self._valid_repair_lineage(store, lineage):
            raise CandidateStaleAuthority("accepted candidate repair lineage is invalid")
        return {"accepted_lineage": lineage.model_dump(mode="json")}

    async def _conflict(self, state, revision, member, partial, evidence, operation_id):
        detail = {
            "batch_id": state["batch"]["id"],
            "revision": int(revision["revision"]),
            "ordinal": int(member["ordinal"]),
            "operation_id": operation_id,
            "operation_stage": int(state["operation"]["active_stage"]),
            "partial_head_sha": partial,
            "source_base_sha": member["source_base_sha"],
            "source_head_sha": member["reviewed_head_sha"],
            "detail": str(evidence),
        }
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, state["project"]["id"])
            await self._validate_authority_on(conn, state, revision=int(revision["revision"]))
            current = (
                await conn.execute(
                    select(integration_candidate_member_results).where(
                        integration_candidate_member_results.c.batch_id == state["batch"]["id"],
                        integration_candidate_member_results.c.revision == revision["revision"],
                        integration_candidate_member_results.c.member_ordinal == member["ordinal"],
                    )
                )
            ).mappings().one_or_none()
            if current is None or current["result"] not in {"pending", "conflict"}:
                raise CandidateStaleAuthority("candidate conflict member changed")
            if current["result"] == "pending":
                member_changed = await conn.execute(
                    update(integration_candidate_member_results)
                    .where(
                        integration_candidate_member_results.c.batch_id == state["batch"]["id"],
                        integration_candidate_member_results.c.revision == revision["revision"],
                        integration_candidate_member_results.c.member_ordinal == member["ordinal"],
                        integration_candidate_member_results.c.result == "pending",
                    )
                    .values(result="conflict", conflict_evidence=detail, updated_at=self.clock())
                )
                if member_changed.rowcount != 1:
                    raise CandidateStaleAuthority("candidate conflict member CAS lost")
            elif current["conflict_evidence"] != detail:
                # A replay must retain the byte-stable first conflict observation.
                detail = current["conflict_evidence"]
                partial = detail["partial_head_sha"]
            batch_changed = await conn.execute(
                update(integration_batches)
                .where(
                    integration_batches.c.id == state["batch"]["id"],
                    integration_batches.c.current_revision == revision["revision"],
                    integration_batches.c.lifecycle.in_(["building", "repairing"]),
                )
                .values(lifecycle="repairing", updated_at=self.clock())
            )
            if batch_changed.rowcount != 1:
                raise CandidateStaleAuthority("candidate conflict batch CAS lost")
        repository = await self._repository(state["batch"]["repository_id"])
        self._assert_repository_binding(repository)
        store = await self._ensure_store(repository)
        token = await self.app_client.installation_token()
        previous = await self._revision(state["batch"]["id"], int(revision["revision"]) - 1)
        expected_old = previous.get("head_sha") if previous else "0" * 40
        published = await self._mutate_ref(
            state,
            revision=int(revision["revision"]),
            purpose="candidate_partial",
            target_branch=state["batch"]["integration_branch"],
            expected_old_sha=expected_old,
            desired_sha=partial,
            store=store,
            token=token,
            member_ordinal=int(member["ordinal"]),
        )
        if not published:
            raise CandidateStaleAuthority("partial candidate publication is waiting")
        await self._crash("after_partial_push")
        await self.repair.dispatch(operation_id, int(state["operation"]["active_stage"]))
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
        if not await self.git.ais_ancestor(
            str(store), member["source_base_sha"], member["reviewed_head_sha"]
        ):
            return False
        tree = await self.git.arun_git_result(
            ["rev-parse", f"{member['reviewed_head_sha']}^{{tree}}"], cwd=str(store)
        )
        return tree.returncode == 0 and tree.stdout.strip() == member["reviewed_tree_sha"]

    @staticmethod
    def _repair_result(outcome, lineage):
        if isinstance(lineage, dict):
            return CandidateRepairResult(
                outcome=outcome,
                batch_id=lineage["batch_id"],
                revision=int(lineage["revision"]),
                member_ordinal=int(lineage["member_ordinal"]),
            )
        return CandidateRepairResult(
            outcome=outcome,
            batch_id=lineage.batch_id,
            revision=lineage.revision,
            member_ordinal=lineage.member_ordinal,
        )

    async def _resolution(self, reservation_id: str):
        async with self.db._engine.connect() as conn:
            row = await self._resolution_on(conn, reservation_id)
        return dict(row) if row else None

    @staticmethod
    async def _resolution_on(conn, reservation_id: str):
        return (
            await conn.execute(
                select(integration_candidate_resolutions).where(
                    integration_candidate_resolutions.c.id == reservation_id
                )
            )
        ).mappings().one_or_none()

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

    async def _has_live_mutation(self, batch_id: str, revision: int) -> bool:
        async with self.db._engine.connect() as conn:
            value = (
                await conn.execute(
                    select(integration_candidate_ref_mutations.c.id).where(
                        integration_candidate_ref_mutations.c.batch_id == batch_id,
                        integration_candidate_ref_mutations.c.revision == revision,
                        integration_candidate_ref_mutations.c.state == "reserved",
                        integration_candidate_ref_mutations.c.expires_at > self.clock(),
                    )
                )
            ).scalar_one_or_none()
        return value is not None

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

    def _assert_repository_binding(self, repository) -> None:
        binding = self.app_client.repository
        expected = f"https://github.com/{binding.full_name}.git"
        if not getattr(self.git, "trusted_local", False) and repository.url != expected:
            raise CandidateAuthorizationError(
                "candidate App repository does not match the frozen repository"
            )

    async def _ensure_store(self, repository):
        digest = hashlib.sha256(repository.id.encode()).hexdigest()
        store = self.data_dir / "integration-repositories" / f"{digest}.git"
        store.parent.mkdir(parents=True, exist_ok=True)
        if not store.exists():
            result = await self.git.arun_git_result(
                ["init", "--bare", "--template=", str(store)], cwd=str(store.parent)
            )
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr or "candidate retained store initialization failed"
                )
        return store

    async def _fetch_oid(self, store: Path, oid: str, destination_ref: str) -> None:
        token = await self.app_client.installation_token()
        await self.git.afetch_exact_oid_with_app_auth(
            str(store),
            repository=self.app_client.repository,
            token=token,
            oid=oid,
            destination_ref=destination_ref,
        )

    async def _fetch_inputs(self, store: Path, state) -> None:
        digest = hashlib.sha256(state["batch"]["id"].encode()).hexdigest()
        await self._fetch_oid(
            store, state["batch"]["base_sha"], f"refs/aq/integration-bases/{digest}"
        )
        for member in state["members"]:
            ordinal = int(member["ordinal"])
            await self._fetch_oid(
                store,
                member["source_base_sha"],
                f"refs/aq/integration-sources/{digest}/{ordinal}/base",
            )
            await self._fetch_oid(
                store,
                member["reviewed_head_sha"],
                f"refs/aq/integration-sources/{digest}/{ordinal}/head",
            )

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
