"""Crash-recoverable child-to-parent squash promotion."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.commands.principal import (
    PrincipalKind,
    TRUSTED_LOCAL,
    current_principal,
    matches_session_instance,
)
from src.git.manager import GitError, GitManager, RemoteRefState
from src.integration.models import ConflictResolutionInput, Fence, PromotionInput, PromotionValue
from src.integration.ownership import BranchOwnership
from src.models import RepoConfig, RepoSourceType
from src.playbooks.invocation import current_invocation


_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_COAUTHOR_RE = re.compile(
    r"^Co-authored-by:\s*(?P<name>.+?)\s*<(?P<email>[^<>\s]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_IDENTITY_NAMESPACE = uuid.UUID("44f3c614-4c2c-4a03-a76d-33575d722b8d")
_INTEGRATION_NAME = "Agent Queue Integration"
_INTEGRATION_EMAIL = "integration@agent-queue.invalid"
_MAX_CONFLICT_BYTES = 65536


class PromotionError(RuntimeError):
    """Base failure with a deterministic command outcome."""


class PromotionConflict(PromotionError):
    def __init__(self, value: PromotionValue, diagnostics: dict[str, Any]):
        super().__init__("reviewed source conflicts with the expected target")
        self.value = value
        self.diagnostics = diagnostics


class PromotionSourceMoved(PromotionError):
    pass


class PromotionTargetMoved(PromotionError):
    pass


class PromotionNotApplied(PromotionError):
    pass


class PromotionInvariantError(PromotionError):
    pass


class PromotionRuntimeError(PromotionError):
    pass


class PromotionAuthorizationError(PromotionError):
    pass


@dataclass(frozen=True)
class ResolvedRepository:
    repo: RepoConfig
    origin_url: str
    retained_git_dir: Path


RepositoryResolver = Callable[[str], Awaitable[RepoConfig | None] | RepoConfig | None]
CrashHook = Callable[[str], Awaitable[None] | None]


class PromotionService:
    """Prepare once, push by exact lease, and recover receipts by ancestry."""

    def __init__(
        self,
        db,
        *,
        data_dir: str | Path,
        git_manager: GitManager | None = None,
        repository_resolver: RepositoryResolver | None = None,
        ownership: BranchOwnership | None = None,
        crash_hook: CrashHook | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.git = git_manager or GitManager()
        self.repository_resolver = repository_resolver
        self.ownership = ownership or BranchOwnership(db)
        self.crash_hook = crash_hook
        self.clock = clock

    async def prepare(self, request: PromotionInput) -> PromotionValue:
        route = await self._validated_route(request)
        domain_key = self._domain_key(request)
        intent_id = f"intent-{uuid.uuid5(_IDENTITY_NAMESPACE, domain_key)}"
        receipt_id = f"receipt-{uuid.uuid5(_IDENTITY_NAMESPACE, 'receipt:' + domain_key)}"
        existing = await self.db.get_integration_promotion_intent(intent_id)
        if existing is not None:
            self._assert_existing_request(existing, request, route, domain_key, receipt_id)
            value = self._value(existing)
            if existing["state"] == "committed" or existing["prepared_sha"] is not None:
                return value
            if existing["state"] == "conflict":
                raise PromotionConflict(value, existing.get("conflict_diagnostics") or {})

        context = await self._validated_context(request, route)
        await self.ownership.assert_current(request.fence)
        repository = await self._resolve_repository(request.fence.target.repository_id)
        if repository.repo.project_id != context["project_id"]:
            raise PromotionInvariantError("repository and task projects do not match")
        await self._ensure_retained_repository(repository)

        async with self.git.arepository_transaction(str(repository.retained_git_dir)):
            await self._fetch_all_heads(repository.retained_git_dir)
            await self._assert_remote_source(
                repository.retained_git_dir,
                context["source_branch"],
                request.source_head,
            )
            await self._assert_commit_inputs(repository.retained_git_dir, request)
            reviewed_tree = await self._tree_oid(repository.retained_git_dir, request.source_head)
            if reviewed_tree != context["review_evidence"]["reviewed_tree_sha"]:
                raise PromotionSourceMoved("reviewed source tree does not match trusted evidence")
            await self._assert_remote_target(
                repository.retained_git_dir,
                request.fence.target.branch,
                request.expected_target,
            )
            authors = await self._authors(
                repository.retained_git_dir, request.source_base, request.source_head
            )

            created_at = float(int(self.clock()))
            primary = (
                authors[0]
                if authors
                else {
                    "name": _INTEGRATION_NAME,
                    "email": _INTEGRATION_EMAIL,
                }
            )
            message = self._message(request.source_task_id, intent_id, receipt_id, authors)
            commit_metadata = {
                "message": message,
                "author": primary,
                "committer": {"name": _INTEGRATION_NAME, "email": _INTEGRATION_EMAIL},
                "timestamp": int(created_at),
            }
            provenance = await self._provenance(
                context["review_evidence"], operation_id=request.operation_key
            )
            try:
                intent = await self.db.reserve_integration_promotion_intent(
                    {
                        "id": intent_id,
                        "domain_key": domain_key,
                        "operation_key": request.operation_key,
                        "project_id": context["project_id"],
                        "receipt_id": receipt_id,
                        "source_task_id": request.source_task_id,
                        "target_task_id": context["target_task_id"],
                        "source_head": request.source_head,
                        "source_base": request.source_base,
                        "repository_id": request.fence.target.repository_id,
                        "origin_url": repository.origin_url,
                        "target_branch": request.fence.target.branch,
                        "expected_target": request.expected_target,
                        "fence_owner_id": request.fence.owner_id,
                        "fence_token": request.fence.token,
                        "review_evidence": context["review_evidence"],
                        "authors": authors,
                        "provenance": provenance,
                        "commit_metadata": commit_metadata,
                        "created_at": created_at,
                    }
                )
            except ValueError as exc:
                if "unresolved promotion" in str(exc):
                    raise PromotionTargetMoved(str(exc)) from exc
                raise PromotionInvariantError(str(exc)) from exc
            value = self._value(intent)
            if intent["state"] == "committed" or intent["prepared_sha"] is not None:
                return value
            if intent["state"] == "conflict":
                raise PromotionConflict(value, intent.get("conflict_diagnostics") or {})

            result = await self.git.arun_git_result(
                [
                    "merge-tree",
                    "--write-tree",
                    f"--merge-base={intent['source_base']}",
                    intent["expected_target"],
                    intent["source_head"],
                ],
                cwd=str(repository.retained_git_dir),
                env={"LC_ALL": "C"},
                lock_held=True,
            )
            if result.returncode == 1:
                diagnostics = self._conflict_diagnostics(intent, result.stdout, result.stderr)
                await self.db.mark_integration_promotion_conflict(intent["id"], diagnostics)
                raise PromotionConflict(self._value(intent), diagnostics)
            if result.returncode != 0:
                raise PromotionRuntimeError(
                    (result.stderr or result.stdout or "git merge-tree failed").strip()
                )
            tree_oid = self._clean_tree_oid(result.stdout)
            await self._assert_object_type(repository.retained_git_dir, tree_oid, "tree")

            metadata = intent["commit_metadata"]
            author = metadata["author"]
            committer = metadata["committer"]
            git_date = f"{int(metadata['timestamp'])} +0000"
            commit = await self.git.arun_git_result(
                ["commit-tree", tree_oid, "-p", intent["expected_target"]],
                cwd=str(repository.retained_git_dir),
                stdin=metadata["message"] + "\n",
                env={
                    "GIT_AUTHOR_NAME": author["name"],
                    "GIT_AUTHOR_EMAIL": author["email"],
                    "GIT_AUTHOR_DATE": git_date,
                    "GIT_COMMITTER_NAME": committer["name"],
                    "GIT_COMMITTER_EMAIL": committer["email"],
                    "GIT_COMMITTER_DATE": git_date,
                    "LC_ALL": "C",
                },
                lock_held=True,
            )
            if commit.returncode != 0 or not _OID_RE.fullmatch(commit.stdout.strip().lower()):
                raise PromotionRuntimeError(
                    (commit.stderr or commit.stdout or "git commit-tree failed").strip()
                )
            prepared_sha = commit.stdout.strip().lower()
            await self._crash("after_object")
            recovery_ref = f"refs/aq/integration-intents/{intent['id']}"
            await self._pin_recovery_ref(repository.retained_git_dir, recovery_ref, prepared_sha)
            await self._crash("after_recovery_ref")
            intent = await self.db.mark_integration_promotion_prepared(
                intent["id"], prepared_sha=prepared_sha, recovery_ref=recovery_ref
            )

        await self._crash("after_prepare")
        return self._value(intent)

    async def push(self, intent_id: str, fence: Fence) -> PromotionValue:
        intent = await self._intent(intent_id)
        if intent["state"] == "committed":
            return self._value(intent)
        if intent["state"] == "conflict":
            raise PromotionConflict(self._value(intent), intent.get("conflict_diagnostics") or {})
        if (
            fence.target.repository_id != intent["repository_id"]
            or fence.target.branch != intent["target_branch"]
        ):
            raise PromotionTargetMoved("push fence targets another branch")
        repository = await self._resolve_repository(intent["repository_id"])
        self._assert_frozen_repository(intent, repository)

        async with self.git.arepository_transaction(str(repository.retained_git_dir)):
            await self._assert_remote_source(
                repository.retained_git_dir,
                intent["provenance"]["source_branch"],
                intent["source_head"],
            )
            remote = await self.git.als_remote_ref(
                str(repository.retained_git_dir), intent["target_branch"]
            )
            if remote.state is RemoteRefState.ERROR:
                raise PromotionRuntimeError(remote.error or "target remote state is unknown")
            if remote.state is RemoteRefState.ABSENT:
                raise PromotionTargetMoved("target branch is absent")
            if remote.oid != intent["expected_target"]:
                if await self._prepared_reachable(repository.retained_git_dir, intent, remote.oid):
                    return await self._finalize(intent, remote.oid)
                raise PromotionTargetMoved("target branch moved from the prepared old tip")

            # The ownership row stays locked only across the actual remote
            # mutation.  A transfer therefore cannot pass after validation
            # but before the lease-protected push.  Terminal/reconciliation
            # replays above remain read-only and intentionally need no live
            # collector fence.
            async with self.ownership.mutation_exclusion(
                fence, expected_role="collector"
            ):
                await self._crash("before_push")
                try:
                    await self.git.apush_expected_delivery(
                        str(repository.retained_git_dir),
                        intent["source_base"],
                        intent["prepared_sha"],
                        intent["target_branch"],
                        intent["expected_target"],
                        lock_held=True,
                    )
                except GitError as exc:
                    raise PromotionRuntimeError(str(exc)) from exc
                await self._crash("after_push")

        remote_evidence = {
            "kind": "exact_tip",
            "remote_sha": intent["prepared_sha"],
        }
        await self.db.mark_integration_promotion_pushed(intent_id, remote_evidence)
        return await self._finalize(intent, intent["prepared_sha"])

    async def reserve_resolution(
        self, request: ConflictResolutionInput
    ) -> tuple[PromotionValue, bool]:
        """Freeze a repair writer's exact resolution before any remote mutation."""
        for label, oid in (
            ("resolved head", request.resolved_head_sha),
            ("resolved tree", request.resolved_tree_sha),
            *(("repair commit", oid) for oid in request.repair_commit_shas),
        ):
            if not _OID_RE.fullmatch(oid):
                raise PromotionInvariantError(f"invalid {label} OID")
        if len(set(request.repair_commit_shas)) != len(request.repair_commit_shas):
            raise PromotionInvariantError("repair commit range contains duplicates")
        if request.repair_commit_shas[-1] != request.resolved_head_sha:
            raise PromotionInvariantError("repair commit range must end at resolved head")
        principal = current_principal()
        if principal is None or principal.kind is not PrincipalKind.SESSION:
            raise PromotionAuthorizationError("conflict resolution requires a repair session")
        if (
            principal.task_id is None
            or principal.session_id is None
            or principal.project_id is None
            or principal.session_instance_token is None
        ):
            raise PromotionAuthorizationError("repair session identity is incomplete")

        intent = await self._intent(request.intent_id)
        if intent["operation_key"] != request.operation_id:
            raise PromotionInvariantError("resolution operation does not match original intent")
        if (
            request.fence.target.repository_id != intent["repository_id"]
            or request.fence.target.branch != intent["target_branch"]
        ):
            raise PromotionInvariantError("resolution fence targets another branch")
        repository = await self._resolve_repository(intent["repository_id"])
        self._assert_resolution_repository(intent, repository)

        async with self.db.immediate() as conn:
            scope = await self.db.get_repair_filing_scope(
                principal.task_id, session_id=principal.session_id, conn=conn
            )
            if (
                scope is None
                or not scope["active"]
                or scope["operation_id"] != request.operation_id
                or scope["target_kind"] != "parent"
                or scope["parent_task_id"] != intent["target_task_id"]
                or scope["project_id"] != intent["project_id"]
                or scope["repository_id"] != intent["repository_id"]
                or scope["writer_kind"] != "repair_delegate"
                or scope["session_id"] != principal.session_id
                or scope["workspace_id"] is None
                or not scope["instance_token"]
                or not matches_session_instance(principal, scope["instance_token"])
                or scope["fence_token"] != request.fence.token
                or request.fence.owner_id != principal.task_id
                or scope["deadline_at"] is None
                or self.clock() >= float(scope["deadline_at"])
            ):
                raise PromotionTargetMoved("repair resolution authority is stale")
            async with self.ownership.mutation_exclusion_on(
                conn, request.fence, state="attached", expected_role="repair"
            ):
                reserved = await self.db.reserve_integration_conflict_resolution(
                    conn,
                    request.intent_id,
                    {
                        "resolved_head_sha": request.resolved_head_sha,
                        "resolved_tree_sha": request.resolved_tree_sha,
                        "repair_commit_shas": list(request.repair_commit_shas),
                        "operation_id": request.operation_id,
                        "stage_ordinal": scope["stage"],
                        "repair_task_id": principal.task_id,
                        "repair_session_id": principal.session_id,
                        "repair_session_instance_token": scope["instance_token"],
                        "repair_workspace_id": scope["workspace_id"],
                        "fence_owner_id": request.fence.owner_id,
                        "fence_token": request.fence.token,
                    },
                )
        return self._value(reserved), bool(reserved.get("_resolution_replayed"))

    async def push_resolution(
        self, intent_id: str, fence: Fence
    ) -> tuple[PromotionValue, bool]:
        """Push only a previously frozen resolution under the current repair writer."""
        principal = current_principal()
        if principal is None or principal.kind is not PrincipalKind.SESSION:
            raise PromotionAuthorizationError("resolution push requires a repair session")
        if (
            principal.task_id is None
            or principal.session_id is None
            or principal.project_id is None
            or principal.session_instance_token is None
        ):
            raise PromotionAuthorizationError("repair session identity is incomplete")
        intent = await self._intent(intent_id)
        if intent["state"] not in {"resolution_reserved", "committed"}:
            raise PromotionInvariantError("promotion has no reserved conflict resolution")
        if (
            fence.target.repository_id != intent["repository_id"]
            or fence.target.branch != intent["target_branch"]
        ):
            raise PromotionTargetMoved("resolution push fence targets another branch")
        repository = await self._resolve_repository(intent["repository_id"])
        self._assert_resolution_repository(intent, repository)

        await self._crash("before_resolution_authority_recheck")
        async with self.db.immediate() as conn:
            scope = await self.db.get_repair_filing_scope(
                principal.task_id, session_id=principal.session_id, conn=conn
            )
            if (
                scope is None
                or not scope["active"]
                or scope["operation_id"] != intent["resolution_operation_id"]
                or scope["target_kind"] != "parent"
                or scope["parent_task_id"] != intent["target_task_id"]
                or scope["project_id"] != intent["project_id"]
                or scope["repository_id"] != intent["repository_id"]
                or scope["writer_kind"] != "repair_delegate"
                or scope["session_id"] != principal.session_id
                or scope["workspace_id"] is None
                or scope["workspace_path"] is None
                or not scope["instance_token"]
                or not matches_session_instance(principal, scope["instance_token"])
                or scope["fence_token"] != fence.token
                or fence.owner_id != principal.task_id
                or scope["deadline_at"] is None
                or self.clock() >= float(scope["deadline_at"])
            ):
                raise PromotionTargetMoved("repair resolution push authority is stale")
            if intent["state"] == "committed":
                return self._value(intent), True
            async with self.ownership.mutation_exclusion_on(
                conn, fence, state="attached", expected_role="repair"
            ):
                workspace = Path(scope["workspace_path"])
                async with self.git.arepository_transaction(str(workspace)):
                    await self._assert_exact_resolution(workspace, intent)
                    remote = await self.git.als_remote_ref(
                        str(workspace),
                        intent["target_branch"],
                        remote=intent["origin_url"],
                    )
                    if remote.state is RemoteRefState.ERROR:
                        raise PromotionRuntimeError(
                            remote.error or "target remote state is unknown"
                        )
                    if remote.state is RemoteRefState.ABSENT:
                        raise PromotionTargetMoved("target branch is absent")
                    already_applied = remote.oid == intent["resolution_head_sha"]
                    if not already_applied:
                        if remote.oid != intent["expected_target"]:
                            raise PromotionTargetMoved(
                                "target branch moved from the resolution old tip"
                            )
                        await self._crash("before_resolution_push")
                        try:
                            await self.git.apush_expected_delivery(
                                str(workspace),
                                intent["expected_target"],
                                intent["resolution_head_sha"],
                                intent["target_branch"],
                                intent["expected_target"],
                                lock_held=True,
                                remote=intent["origin_url"],
                            )
                        except GitError as exc:
                            raise PromotionRuntimeError(str(exc)) from exc
                        await self._crash("after_resolution_push")
                await self.db.record_integration_resolution_push_on(
                    conn,
                    intent_id,
                    {
                        "kind": "exact_resolution_push_observed",
                        "remote_sha": intent["resolution_head_sha"],
                        "operation_id": scope["operation_id"],
                        "stage_ordinal": scope["stage"],
                        "repair_task_id": principal.task_id,
                        "repair_session_id": principal.session_id,
                        "repair_session_instance_token": scope["instance_token"],
                        "repair_workspace_id": scope["workspace_id"],
                        "fence_owner_id": fence.owner_id,
                        "fence_token": fence.token,
                    },
                )
        return self._value(intent), already_applied

    async def reconcile(self, intent_id: str) -> PromotionValue:
        intent = await self._intent(intent_id)
        if intent["state"] == "committed":
            return self._value(intent)
        if intent["state"] == "conflict":
            raise PromotionConflict(self._value(intent), intent.get("conflict_diagnostics") or {})
        if intent["state"] == "resolution_reserved":
            return await self._reconcile_resolution(intent)
        if not intent["prepared_sha"]:
            raise PromotionInvariantError("promotion intent has no prepared commit")
        repository = await self._resolve_repository(intent["repository_id"])
        self._assert_frozen_repository(intent, repository)
        async with self.git.arepository_transaction(str(repository.retained_git_dir)):
            remote = await self.git.als_remote_ref(
                str(repository.retained_git_dir), intent["target_branch"]
            )
            if remote.state is RemoteRefState.ERROR:
                raise PromotionRuntimeError(remote.error or "target remote state is unknown")
            if remote.state is RemoteRefState.ABSENT:
                raise PromotionInvariantError("target branch disappeared during reconciliation")
            if remote.oid == intent["expected_target"]:
                raise PromotionNotApplied("prepared push has not been applied")
            if not await self._prepared_reachable(repository.retained_git_dir, intent, remote.oid):
                raise PromotionInvariantError("target diverged from the prepared promotion")
        return await self._finalize(intent, remote.oid)

    async def _reconcile_resolution(self, intent: dict[str, Any]) -> PromotionValue:
        repository = await self._resolve_repository(intent["repository_id"])
        self._assert_frozen_repository(intent, repository)
        async with self.git.arepository_transaction(str(repository.retained_git_dir)):
            remote = await self.git.als_remote_ref(
                str(repository.retained_git_dir), intent["target_branch"]
            )
            if remote.state is RemoteRefState.ERROR:
                raise PromotionRuntimeError(remote.error or "target remote state is unknown")
            if remote.state is RemoteRefState.ABSENT:
                raise PromotionInvariantError("target branch disappeared during reconciliation")
            if remote.oid == intent["expected_target"]:
                raise PromotionNotApplied("reserved resolution push has not been applied")
            if remote.oid != intent["resolution_head_sha"]:
                raise PromotionInvariantError("target diverged from the reserved resolution")
            recovery_ref = f"refs/aq/integration-intents/{intent['id']}"
            fetch = await self.git.arun_git_result(
                [
                    "fetch",
                    "--no-tags",
                    "origin",
                    f"+refs/heads/{intent['target_branch']}:{recovery_ref}",
                ],
                cwd=str(repository.retained_git_dir),
                env={"LC_ALL": "C"},
                lock_held=True,
            )
            if fetch.returncode != 0:
                raise PromotionRuntimeError((fetch.stderr or "target fetch failed").strip())
            fetched = await self.git.arun_git_result(
                ["rev-parse", "--verify", recovery_ref],
                cwd=str(repository.retained_git_dir),
                env={"LC_ALL": "C"},
                lock_held=True,
            )
            if fetched.returncode != 0 or fetched.stdout.strip() != remote.oid:
                raise PromotionRuntimeError("target moved while resolution was fetched")
            await self._assert_exact_resolution(repository.retained_git_dir, intent)
        remote_evidence = {
            "kind": "exact_resolution_tip",
            "remote_sha": intent["resolution_head_sha"],
            "resolved_tree_sha": intent["resolution_tree_sha"],
            "repair_commit_shas": intent["resolution_commit_shas"],
        }
        await self.db.finalize_integration_promotion(intent["id"], remote_evidence)
        await self._crash("before_outbox_ack")
        return self._value(await self._intent(intent["id"]))

    async def _assert_exact_resolution(self, store: Path, intent: dict[str, Any]) -> None:
        await self._assert_object_type(store, intent["expected_target"], "commit")
        await self._assert_object_type(store, intent["resolution_head_sha"], "commit")
        if not await self._is_ancestor(
            store, intent["expected_target"], intent["resolution_head_sha"]
        ):
            raise PromotionInvariantError("resolution head is not descended from expected target")
        tree = await self._tree_oid(store, intent["resolution_head_sha"])
        if tree != intent["resolution_tree_sha"]:
            raise PromotionInvariantError("resolution tree does not match reservation")
        commits = await self._resolution_commit_range(
            store, intent["expected_target"], intent["resolution_head_sha"]
        )
        if not commits or commits != intent["resolution_commit_shas"]:
            raise PromotionInvariantError("resolution commit range does not match reservation")
        merges = await self.git.arun_git_result(
            [
                "rev-list",
                "--min-parents=2",
                f"{intent['expected_target']}..{intent['resolution_head_sha']}",
            ],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if merges.returncode != 0:
            raise PromotionRuntimeError((merges.stderr or "merge scan failed").strip())
        if merges.stdout.strip():
            raise PromotionInvariantError("resolution commit range contains a merge commit")

    async def _resolution_commit_range(
        self, store: Path, expected_target: str, resolved_head: str
    ) -> list[str]:
        result = await self.git.arun_git_result(
            ["rev-list", "--reverse", f"{expected_target}..{resolved_head}"],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or any(not _OID_RE.fullmatch(oid) for oid in commits):
            raise PromotionRuntimeError((result.stderr or "commit range failed").strip())
        return commits

    async def _validated_route(self, request: PromotionInput) -> dict[str, Any]:
        for label, oid in (
            ("source head", request.source_head),
            ("source base", request.source_base),
            ("expected target", request.expected_target),
        ):
            if not isinstance(oid, str) or not _OID_RE.fullmatch(oid.lower()):
                raise PromotionSourceMoved(f"invalid {label} OID")
        task = await self.db.get_task(request.source_task_id)
        if task is None or not task.parent_task_id or not task.repo_id or not task.branch_name:
            raise PromotionSourceMoved("source task has no materialized parent delivery identity")
        parent = await self.db.get_task(task.parent_task_id)
        if (
            parent is None
            or parent.project_id != task.project_id
            or parent.repo_id != task.repo_id
            or not parent.branch_name
        ):
            raise PromotionSourceMoved("source task parent identity is invalid")
        target = request.fence.target
        if target.repository_id != task.repo_id or target.branch != parent.branch_name:
            raise PromotionSourceMoved("promotion target is not the source task's immediate parent")
        repo = await self._get_repo(target.repository_id)
        if (
            repo is None
            or repo.project_id != task.project_id
            or target.branch == repo.default_branch
        ):
            raise PromotionSourceMoved("target repository is not the task's parent repository")
        return {
            "project_id": task.project_id,
            "target_task_id": parent.id,
            "source_branch": task.branch_name,
            "task": task,
            "parent": parent,
            "repository": repo,
        }

    async def _validated_context(
        self, request: PromotionInput, route: dict[str, Any]
    ) -> dict[str, Any]:
        task = route["task"]
        parent = route["parent"]
        origin = await self.db.get_task_branch_origin_for_promotion(task.id, task.repo_id)
        if (
            origin is None
            or not origin["reserved"]
            or not origin["materialized"]
            or origin["parent_task_id"] != parent.id
            or origin["parent_repository_id"] != parent.repo_id
            or origin["parent_ref"] != parent.branch_name
            or origin["base_sha"] != request.source_base
        ):
            raise PromotionSourceMoved("task branch origin is absent, moved, or retired")
        checkpoint = await self.db.get_integration_checkpoint(task.id)
        current_generation = int(checkpoint["generation"]) if checkpoint is not None else 0
        review = await self.db.get_applicable_integration_review_evidence(
            source_task_id=task.id,
            repository_id=task.repo_id,
            source_base=request.source_base,
            reviewed_head_sha=request.source_head,
            current_generation=current_generation,
        )
        if review is None:
            raise PromotionSourceMoved("trusted review evidence is absent or superseded")
        return dict(route) | {"review_evidence": review}

    async def _get_repo(self, repository_id: str) -> RepoConfig | None:
        result = (
            self.repository_resolver(repository_id)
            if self.repository_resolver is not None
            else self.db.get_repo(repository_id)
        )
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _resolve_repository(self, repository_id: str) -> ResolvedRepository:
        repo = await self._get_repo(repository_id)
        if repo is None or repo.id != repository_id:
            raise PromotionInvariantError("canonical repository is not configured")
        origin = repo.url
        if not origin and repo.source_type is RepoSourceType.LINK:
            source = Path(repo.source_path)
            if not source.is_dir():
                raise PromotionInvariantError("linked repository source path is unavailable")
            result = await self.git.arun_git_result(
                ["remote", "get-url", "origin"], cwd=str(source), env={"LC_ALL": "C"}
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise PromotionInvariantError("linked repository origin is unavailable")
            origin = result.stdout.strip()
        if not origin:
            raise PromotionInvariantError("canonical repository has no immutable origin")
        digest = hashlib.sha256(repository_id.encode("utf-8")).hexdigest()
        return ResolvedRepository(
            repo=repo,
            origin_url=origin,
            retained_git_dir=self.data_dir / "integration-repositories" / f"{digest}.git",
        )

    async def _ensure_retained_repository(self, repository: ResolvedRepository) -> None:
        store = repository.retained_git_dir
        store.parent.mkdir(parents=True, exist_ok=True)
        async with self.git.arepository_transaction(str(store)):
            if not store.exists():
                result = await self.git.arun_git_result(
                    ["clone", "--bare", "--", repository.origin_url, str(store)],
                    cwd=str(store.parent),
                    env={"LC_ALL": "C"},
                    lock_held=True,
                )
                if result.returncode != 0:
                    raise PromotionRuntimeError(
                        (result.stderr or result.stdout or "retained clone failed").strip()
                    )
            bare = await self.git.arun_git_result(
                ["rev-parse", "--is-bare-repository"],
                cwd=str(store),
                env={"LC_ALL": "C"},
                lock_held=True,
            )
            configured = await self.git.arun_git_result(
                ["remote", "get-url", "origin"],
                cwd=str(store),
                env={"LC_ALL": "C"},
                lock_held=True,
            )
            if (
                bare.returncode != 0
                or bare.stdout.strip() != "true"
                or configured.returncode != 0
                or configured.stdout.strip() != repository.origin_url
            ):
                raise PromotionInvariantError("retained repository identity changed")

    async def _fetch_all_heads(self, store: Path) -> None:
        result = await self.git.arun_git_result(
            ["fetch", "--no-tags", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if result.returncode != 0:
            raise PromotionRuntimeError((result.stderr or "git fetch failed").strip())

    async def _assert_remote_source(self, store: Path, branch: str, expected: str) -> None:
        result = await self.git.als_remote_ref(str(store), branch)
        if result.state is RemoteRefState.ERROR:
            raise PromotionRuntimeError(result.error or "source remote state is unknown")
        if result.state is not RemoteRefState.PRESENT or result.oid != expected:
            raise PromotionSourceMoved("source branch moved from the reviewed head")

    async def _assert_remote_target(self, store: Path, branch: str, expected: str) -> None:
        result = await self.git.als_remote_ref(str(store), branch)
        if result.state is RemoteRefState.ERROR:
            raise PromotionRuntimeError(result.error or "target remote state is unknown")
        if result.state is not RemoteRefState.PRESENT or result.oid != expected:
            raise PromotionTargetMoved("target branch moved from the expected tip")

    async def _assert_commit_inputs(self, store: Path, request: PromotionInput) -> None:
        for oid in (request.source_base, request.source_head, request.expected_target):
            await self._assert_object_type(store, oid, "commit")
        if not await self._is_ancestor(store, request.source_base, request.source_head):
            raise PromotionSourceMoved("reviewed source is not descended from its recorded base")
        if not await self._is_ancestor(store, request.source_base, request.expected_target):
            raise PromotionTargetMoved("expected target is not descended from the source base")

    async def _is_ancestor(self, store: Path, ancestor: str, descendant: str) -> bool:
        result = await self.git.arun_git_result(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if result.returncode not in {0, 1}:
            raise PromotionRuntimeError((result.stderr or "ancestry check failed").strip())
        return result.returncode == 0

    async def _assert_object_type(self, store: Path, oid: str, expected_type: str) -> None:
        result = await self.git.arun_git_result(
            ["cat-file", "-t", oid], cwd=str(store), env={"LC_ALL": "C"}, lock_held=True
        )
        if result.returncode != 0 or result.stdout.strip() != expected_type:
            raise PromotionSourceMoved(f"promotion input is not a {expected_type} object")

    async def _tree_oid(self, store: Path, commit: str) -> str:
        result = await self.git.arun_git_result(
            ["rev-parse", f"{commit}^{{tree}}"],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        oid = result.stdout.strip().lower()
        if result.returncode != 0 or not _OID_RE.fullmatch(oid):
            raise PromotionSourceMoved("reviewed source tree cannot be resolved")
        return oid

    async def _authors(self, store: Path, base: str, head: str) -> list[dict[str, str]]:
        result = await self.git.arun_git_result(
            ["log", "--format=%an%x00%ae%x00%B%x00%x1e", f"{base}..{head}"],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if result.returncode != 0:
            raise PromotionRuntimeError((result.stderr or "git log failed").strip())
        identities: set[tuple[str, str]] = set()
        for record in result.stdout.split("\x1e"):
            fields = record.strip("\n\x00").split("\x00", 2)
            if len(fields) != 3:
                continue
            self._add_identity(identities, fields[0], fields[1])
            for match in _COAUTHOR_RE.finditer(fields[2]):
                self._add_identity(identities, match.group("name"), match.group("email"))
        return [
            {"name": name, "email": email}
            for email, name in sorted((email, name) for name, email in identities)
        ]

    @staticmethod
    def _add_identity(identities: set[tuple[str, str]], name: str, email: str) -> None:
        normalized_name = " ".join(name.split())
        normalized_email = email.strip().lower()
        if normalized_name and "@" in normalized_email and "\n" not in normalized_email:
            identities.add((normalized_name, normalized_email))

    @staticmethod
    def _message(
        task_id: str, intent_id: str, receipt_id: str, authors: list[dict[str, str]]
    ) -> str:
        body = f"Integrate task {task_id}\n\nAQ-Receipt: {receipt_id}\nAQ-Intent: {intent_id}"
        if len(authors) > 1:
            trailers = "\n".join(
                f"Co-authored-by: {author['name']} <{author['email']}>" for author in authors[1:]
            )
            body += "\n\n" + trailers
        return body

    async def _provenance(
        self, review: dict[str, Any], *, operation_id: str
    ) -> dict[str, Any]:
        principal = current_principal() or TRUSTED_LOCAL
        invocation = current_invocation()
        operation_route = await self.db.get_integration_operation_artifact_route(
            operation_id
        )
        if invocation is not None and operation_route is not None:
            artifact_snapshot = operation_route.get("artifact_snapshot")
            if (
                not isinstance(artifact_snapshot, dict)
                or invocation.artifact_ref.as_dict() != artifact_snapshot
                or operation_route.get("playbook_id")
                != invocation.artifact_ref.playbook_id
                or operation_route.get("artifact_sha256")
                != invocation.artifact_ref.artifact_sha256
            ):
                raise PromotionInvariantError(
                    "playbook invocation artifact does not match the frozen operation route"
                )
        reviewer_attempt = None
        if review.get("reviewer_session_attempt_id"):
            reviewer_attempt = await self.db.get_task_session_attempt(
                review["reviewer_session_attempt_id"]
            )
            if reviewer_attempt is None:
                raise PromotionInvariantError("reviewer session attempt is unavailable")
        return {
            "principal": principal.describe(),
            "kind": principal.kind.value,
            "session_id": principal.session_id,
            "task_id": principal.task_id,
            "project_id": principal.project_id,
            "profile_id": principal.profile_id,
            "service_name": principal.service_name,
            "playbook_run_id": invocation.run_id if invocation else None,
            "playbook_step_id": invocation.step_id if invocation else None,
            "playbook_attempt": invocation.attempt if invocation else None,
            "reviewer_session_attempt": reviewer_attempt,
            "source_branch": (await self.db.get_task(review["source_task_id"])).branch_name,
        }

    @staticmethod
    def _domain_key(request: PromotionInput) -> str:
        encoded = json.dumps(
            [
                request.source_task_id,
                request.source_head.lower(),
                request.fence.target.repository_id,
                request.fence.target.branch,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _assert_existing_request(
        intent: dict,
        request: PromotionInput,
        route: dict[str, Any],
        domain_key: str,
        receipt_id: str,
    ) -> None:
        expected = {
            "domain_key": domain_key,
            "receipt_id": receipt_id,
            "operation_key": request.operation_key,
            "project_id": route["project_id"],
            "source_task_id": request.source_task_id,
            "target_task_id": route["target_task_id"],
            "source_head": request.source_head,
            "source_base": request.source_base,
            "repository_id": request.fence.target.repository_id,
            "target_branch": request.fence.target.branch,
            "expected_target": request.expected_target,
        }
        changed = [field for field, value in expected.items() if intent.get(field) != value]
        if changed:
            raise PromotionInvariantError(
                "promotion intent identity changed: " + ", ".join(changed)
            )

    @staticmethod
    def _clean_tree_oid(stdout: str) -> str:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1 or not _OID_RE.fullmatch(lines[0].lower()):
            raise PromotionInvariantError("clean merge-tree output was not one tree OID")
        return lines[0].lower()

    @staticmethod
    def _conflict_diagnostics(intent: dict, stdout: str, stderr: str) -> dict[str, Any]:
        all_paths = sorted(
            {
                line.split("\t", 1)[1].strip()
                for line in stdout.splitlines()
                if "\t" in line and line.split("\t", 1)[1].strip()
            }
        )
        raw = (stdout + ("\n" if stdout and stderr else "") + stderr).encode(
            "utf-8", errors="replace"
        )
        diagnostics = {
            "base": intent["source_base"],
            "source": intent["source_head"],
            "target": intent["expected_target"],
            "returncode": 1,
            "paths": all_paths,
            "output": raw.decode("utf-8", errors="ignore"),
            "truncated": False,
        }
        def serialized_size() -> int:
            return len(json.dumps(diagnostics, sort_keys=True).encode("utf-8"))

        if serialized_size() <= _MAX_CONFLICT_BYTES:
            return diagnostics

        marker = "[truncated at 65536 serialized UTF-8 bytes]"
        diagnostics.update(paths=[], output=marker, truncated=True)

        low, high = 0, len(all_paths)
        while low < high:
            middle = (low + high + 1) // 2
            diagnostics["paths"] = all_paths[:middle]
            if serialized_size() <= _MAX_CONFLICT_BYTES:
                low = middle
            else:
                high = middle - 1
        diagnostics["paths"] = all_paths[:low]

        low, high = 0, len(raw)
        while low < high:
            middle = (low + high + 1) // 2
            prefix = raw[:middle].decode("utf-8", errors="ignore")
            diagnostics["output"] = f"{prefix}\n{marker}" if prefix else marker
            if serialized_size() <= _MAX_CONFLICT_BYTES:
                low = middle
            else:
                high = middle - 1
        prefix = raw[:low].decode("utf-8", errors="ignore")
        diagnostics["output"] = f"{prefix}\n{marker}" if prefix else marker
        return diagnostics

    async def _pin_recovery_ref(self, store: Path, ref: str, prepared_sha: str) -> None:
        current = await self.git.arun_git_result(
            ["rev-parse", "--verify", ref],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if current.returncode == 0:
            if current.stdout.strip().lower() != prepared_sha:
                raise PromotionInvariantError("recovery ref points to another prepared commit")
            return
        result = await self.git.arun_git_result(
            ["update-ref", ref, prepared_sha, "0" * 40],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if result.returncode != 0:
            raise PromotionRuntimeError((result.stderr or "recovery ref update failed").strip())

    async def _prepared_reachable(self, store: Path, intent: dict, remote_oid: str) -> bool:
        fetch = await self.git.arun_git_result(
            [
                "fetch",
                "--no-tags",
                "origin",
                f"+refs/heads/{intent['target_branch']}:refs/aq/reconcile/{intent['id']}",
            ],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if fetch.returncode != 0:
            raise PromotionRuntimeError((fetch.stderr or "target fetch failed").strip())
        fetched = await self.git.arun_git_result(
            ["rev-parse", f"refs/aq/reconcile/{intent['id']}"],
            cwd=str(store),
            env={"LC_ALL": "C"},
            lock_held=True,
        )
        if fetched.returncode != 0 or fetched.stdout.strip().lower() != remote_oid:
            raise PromotionRuntimeError("target moved while reconciliation fetched it")
        if remote_oid == intent["prepared_sha"]:
            return True
        return await self._is_ancestor(store, intent["prepared_sha"], remote_oid)

    async def _finalize(self, intent: dict, remote_oid: str) -> PromotionValue:
        if intent.get("intent_kind", "child") != "child":
            raise PromotionInvariantError("root intent requires the root-only finalizer")
        await self.db.finalize_integration_promotion(
            intent["id"],
            {"kind": "prepared_reachable", "remote_sha": remote_oid},
        )
        await self._crash("before_outbox_ack")
        committed = await self._intent(intent["id"])
        return self._value(committed)

    async def _intent(self, intent_id: str) -> dict:
        intent = await self.db.get_integration_promotion_intent(intent_id)
        if intent is None:
            raise PromotionInvariantError("promotion intent does not exist")
        return intent

    @staticmethod
    def _assert_frozen_repository(intent: dict, repository: ResolvedRepository) -> None:
        if (
            intent["project_id"] != repository.repo.project_id
            or intent["origin_url"] != repository.origin_url
        ):
            raise PromotionInvariantError("promotion repository identity changed")
        if not repository.retained_git_dir.is_dir():
            raise PromotionInvariantError("retained promotion repository is unavailable")

    @staticmethod
    def _assert_resolution_repository(intent: dict, repository: ResolvedRepository) -> None:
        if (
            intent["project_id"] != repository.repo.project_id
            or intent["origin_url"] != repository.origin_url
        ):
            raise PromotionInvariantError("promotion repository identity changed")

    @staticmethod
    def _value(intent: dict) -> PromotionValue:
        return PromotionValue(
            intent_id=intent["id"],
            receipt_id=intent["receipt_id"],
            prepared_sha=intent.get("prepared_sha"),
        )

    async def _crash(self, phase: str) -> None:
        if self.crash_hook is None:
            return
        result = self.crash_hook(phase)
        if inspect.isawaitable(result):
            await result


__all__ = [
    "PromotionAuthorizationError",
    "PromotionConflict",
    "PromotionError",
    "PromotionInvariantError",
    "PromotionNotApplied",
    "PromotionRuntimeError",
    "PromotionService",
    "PromotionSourceMoved",
    "PromotionTargetMoved",
    "ResolvedRepository",
]
