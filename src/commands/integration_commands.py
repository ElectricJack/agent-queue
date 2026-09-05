"""Command-handler namespace for hierarchical integration primitives.

Handlers are added here only with the task that implements their durable
mechanism.  An absent handler remains an explicit ``Unknown command`` refusal;
there are intentionally no optimistic success stubs.
"""

from __future__ import annotations

from typing import Any

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.git.manager import GitError
from src.git.manager import RemoteRefState
from src.database.queries.hierarchy_queries import HierarchyError
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.models import TaskStatus


_TASK_OWNER_ROLES = frozenset({"worker", "repair", "verifier"})


def _failure(outcome: str, error: str) -> dict[str, Any]:
    return {"success": False, "outcome": outcome, "error": error}


class IntegrationCommandsMixin:
    """Implemented integration command handlers are registered incrementally."""

    async def _integration_task_matches_target(
        self, task_id: str, target: BranchKey, project_id: str
    ) -> bool:
        task = await self.db.get_task(task_id)
        return bool(
            task is not None
            and task.project_id == project_id
            and task.repo_id == target.repository_id
            and task.branch_name == target.branch
        )

    async def _integration_batch_matches_target(
        self, batch_id: str, target: BranchKey, project_id: str
    ) -> bool:
        batch = await self.db.get_integration_batch(batch_id)
        return bool(
            batch is not None
            and batch["project_id"] == project_id
            and batch["repository_id"] == target.repository_id
            and batch["integration_branch"] == target.branch
        )

    async def _integration_collector_matches_target(
        self, owner_id: str, target: BranchKey, project_id: str
    ) -> bool:
        if await self._integration_batch_matches_target(owner_id, target, project_id):
            return True

        operation = await self.db.get_integration_operation(owner_id)
        if operation is None:
            return False
        return await self._integration_operation_matches_target(operation, target, project_id)

    async def _integration_operation_matches_target(
        self, operation: dict, target: BranchKey, project_id: str
    ) -> bool:
        if operation["target_kind"] == "batch":
            batch_id = operation.get("batch_id")
            return bool(
                batch_id
                and await self._integration_batch_matches_target(batch_id, target, project_id)
            )
        if operation["target_kind"] == "parent":
            parent_task_id = operation.get("parent_task_id")
            return bool(
                parent_task_id
                and await self._integration_task_matches_target(parent_task_id, target, project_id)
            )
        # Future operation kinds are denied until their target binding is a
        # real persisted relationship this command can resolve.
        return False

    async def _integration_repair_task_matches_target(
        self, task_id: str, target: BranchKey, project_id: str
    ) -> bool:
        task = await self.db.get_task(task_id)
        if (
            task is None
            or task.project_id != project_id
            or task.repo_id != target.repository_id
            or task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
        ):
            return False
        operation = await self.db.get_active_integration_repair_for_task(task_id)
        return bool(
            operation
            and operation.get("writer_kind") == "repair_delegate"
            and await self._integration_operation_matches_target(operation, target, project_id)
        )

    async def _integration_destination_matches_target(
        self, owner_id: str, role: str, target: BranchKey, project_id: str
    ) -> bool:
        if role == "repair":
            return await self._integration_repair_task_matches_target(owner_id, target, project_id)
        if role == "verifier":
            task = await self.db.get_task(owner_id)
            if task is None or task.project_id != project_id or task.repo_id != target.repository_id:
                return False
            operation = await self.db.get_active_integration_verifier_for_task(owner_id)
            if operation is None:
                operation = await self.db.get_active_parent_integration_operation(owner_id)
                if operation is None or operation.get("verifier_task_id") is not None:
                    return False
            return await self._integration_operation_matches_target(
                operation, target, project_id
            )
        if role in _TASK_OWNER_ROLES:
            return await self._integration_task_matches_target(owner_id, target, project_id)
        if role == "collector":
            return await self._integration_collector_matches_target(owner_id, target, project_id)
        return False

    async def _cmd_integration_transfer_owner(self, args: dict) -> dict:
        """Fence out one branch writer only after a proven server-side handoff."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationTransferOwnerArgs

        try:
            request = IntegrationTransferOwnerArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("human_required", f"invalid ownership transfer: {exc}")

        target = request.target
        repository = await self.db.get_repo(target.repository_id)
        if repository is None or await self.db.get_project(repository.project_id) is None:
            return _failure("human_required", "target repository is not configured")

        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SESSION:
            return _failure("human_required", "session principals cannot transfer branch ownership")
        if principal.kind is PrincipalKind.PLAYBOOK:
            explicitly_capable = not principal.unresolved and principal.policy.allows(
                "aq_commands", "integration_transfer_owner"
            )
            if not explicitly_capable or principal.project_id != repository.project_id:
                return _failure(
                    "human_required",
                    "playbook ownership transfer authority is outside the target scope",
                )
        elif principal.kind not in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}:
            return _failure("human_required", "ownership transfer authority is unresolved")

        if not await self._integration_destination_matches_target(
            request.next_owner_id,
            request.next_role,
            target,
            repository.project_id,
        ):
            return _failure(
                "human_required",
                "destination owner is not bound to the target repository branch",
            )

        ownership = BranchOwnership(
            self.db,
            confirm_handoff=getattr(self.orchestrator, "aconfirm_integration_owner_handoff", None),
        )
        current = await ownership.get_owner(target)
        if current is None:
            return _failure("stale_owner", "branch ownership record does not exist")

        current_token = int(current["fence_token"])
        if current_token != request.expected_token:
            # Natural idempotency: a replay after response loss observes the
            # exact successor already installed and returns its stable fence.
            if (
                current_token == request.expected_token + 1
                and current["owner_id"] == request.next_owner_id
                and current["owner_role"] == request.next_role
            ):
                fence = Fence(
                    target=target,
                    owner_id=request.next_owner_id,
                    token=current_token,
                )
                transferred = fence
            else:
                return _failure("stale_owner", "branch ownership fence is stale")
        else:
            fence = Fence(
                target=target,
                owner_id=current["owner_id"],
                token=current_token,
            )
            try:
                if request.next_role == "verifier":
                    operation = await self.db.get_active_integration_verifier_for_task(
                        request.next_owner_id
                    )
                    parent_id = (
                        operation["parent_task_id"]
                        if operation
                        else request.next_owner_id
                    )
                    readiness = await self._hierarchy_integration_service().readiness(
                        parent_id
                    )
                    if readiness["outcome"] != "ready":
                        return _failure(
                            "busy", "parent delivery is not ready for verifier handoff"
                        )
                transferred = await ownership.transfer(
                    fence, request.next_owner_id, request.next_role
                )
            except StaleFence as exc:
                return _failure("stale_owner", str(exc))
            except BranchBusy as exc:
                return _failure("busy", str(exc))
        if request.next_role == "verifier":
            operation = await self.db.get_active_integration_verifier_for_task(
                request.next_owner_id
            )
            parent_id = operation["parent_task_id"] if operation else request.next_owner_id
            try:
                await self._hierarchy_integration_service().wake_verifier(
                    parent_id, transferred
                )
            except HierarchyError as exc:
                return _failure("human_required", str(exc))
        return {
            "success": True,
            "outcome": "transferred",
            "fence": transferred.model_dump(mode="json"),
        }

    async def _integration_delivery_authorized(
        self, project_id: str, capability: str, *, allow_session_read: bool = False
    ) -> bool:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SESSION:
            return bool(allow_session_read and principal.project_id == project_id)
        if principal.kind is PrincipalKind.PLAYBOOK:
            return bool(
                not principal.unresolved
                and principal.project_id == project_id
                and principal.policy.allows("aq_commands", capability)
            )
        return principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}

    def _integration_promotion_service(self):
        service = getattr(self.orchestrator, "promotion_service", None)
        if service is not None:
            return service
        from src.integration.promotion import PromotionService

        return PromotionService(
            self.db,
            data_dir=self.config.data_dir,
            git_manager=self.orchestrator.git,
        )

    def _hierarchy_integration_service(self):
        service = getattr(self.orchestrator, "hierarchy_integration", None)
        if service is not None:
            return service
        from src.integration.hierarchy import (
            HierarchyIntegration,
            materialize_exact_branch,
            verify_workspace_checkpoint,
        )

        async def resolve_head(repo, branch):
            promotion = self._integration_promotion_service()
            resolved = await promotion._resolve_repository(repo.id)
            await promotion._ensure_retained_repository(resolved)
            remote = await promotion.git.als_remote_ref(str(resolved.retained_git_dir), branch)
            if remote.state is RemoteRefState.ERROR:
                raise GitError(remote.error or "repository head state is unknown")
            if remote.state is not RemoteRefState.PRESENT or remote.oid is None:
                raise GitError(f"repository branch {branch!r} does not exist")
            return remote.oid

        async def materialize(repo, branch, base_sha):
            promotion = self._integration_promotion_service()
            resolved = await promotion._resolve_repository(repo.id)
            await promotion._ensure_retained_repository(resolved)
            async with promotion.git.arepository_transaction(
                str(resolved.retained_git_dir)
            ):
                return await materialize_exact_branch(
                    promotion.git, str(resolved.retained_git_dir), branch, base_sha
                )

        async def verify_checkpoint(task, repo, head_sha):
            return await verify_workspace_checkpoint(
                self.db, self.orchestrator.git, task, repo, head_sha
            )

        return HierarchyIntegration(
            self.db,
            default_head_resolver=resolve_head,
            branch_materializer=materialize,
            checkpoint_verifier=verify_checkpoint,
        )

    def _integration_repair_service(self):
        service = getattr(self.orchestrator, "repair_service", None)
        if service is not None:
            return service
        from src.integration.repair import RepairService

        async def route_valid(intelligence_class, profile_id):
            profile = await self.db.get_profile(profile_id) if profile_id else None
            if profile_id and profile is None:
                return False
            return self._validate_routing_class(intelligence_class, profile) is None

        return RepairService(
            self.db,
            confirm_handoff=getattr(
                self.orchestrator, "aconfirm_integration_owner_handoff", None
            ),
            confirm_stopped=getattr(
                self.orchestrator,
                "aconfirm_integration_owner_stopped_for_repair",
                None,
            ),
            route_validator=route_valid,
        )

    async def _integration_operation_project_id(self, operation: dict) -> str | None:
        if operation["target_kind"] == "parent":
            task = await self.db.get_task(operation.get("parent_task_id") or "")
            return task.project_id if task is not None else None
        if operation["target_kind"] == "batch":
            batch = await self.db.get_integration_batch(operation.get("batch_id") or "")
            return str(batch["project_id"]) if batch is not None else None
        return None

    async def _repair_command_authorized(
        self, operation_id: str, capability: str
    ) -> tuple[dict | None, bool]:
        operation = await self.db.get_integration_operation(operation_id)
        if operation is None:
            return None, False
        project_id = await self._integration_operation_project_id(operation)
        if project_id is None:
            return operation, False
        return operation, await self._integration_delivery_authorized(
            project_id, capability
        )

    async def _cmd_integration_repair_start(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRepairStartArgs

        try:
            request = IntegrationRepairStartArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("stale", f"invalid repair start: {exc}")
        _operation, authorized = await self._repair_command_authorized(
            request.operation_id, "integration_repair_start"
        )
        if not authorized:
            return _failure("unauthorized", "repair start authority is outside the operation")
        result = await self._integration_repair_service().start(
            request.operation_id, request.starting_sha, request.trigger_id
        )
        return {"success": result["outcome"] in {"started", "already_started"}, **result}

    async def _cmd_integration_record_repair(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRecordRepairArgs

        try:
            request = IntegrationRecordRepairArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("budget_exhausted", f"invalid repair evidence: {exc}")
        _operation, authorized = await self._repair_command_authorized(
            request.operation_id, "integration_record_repair"
        )
        if not authorized:
            return _failure("unauthorized", "repair evidence authority is outside the operation")
        result = await self._integration_repair_service().record_result(
            request.operation_id, request.evidence_id
        )
        return {
            "success": result["outcome"] in {"continue", "escalate"},
            **result,
        }

    async def _cmd_integration_repair_timeout(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRepairTimeoutArgs

        try:
            request = IntegrationRepairTimeoutArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("stale", f"invalid repair timeout: {exc}")
        _operation, authorized = await self._repair_command_authorized(
            request.operation_id, "integration_repair_timeout"
        )
        if not authorized:
            return _failure("unauthorized", "repair timeout authority is outside the operation")
        result = await self._integration_repair_service().expire(
            request.operation_id, request.stage
        )
        return {"success": result["outcome"] != "stale", **result}

    async def _cmd_integration_repair_dispatch(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRepairDispatchArgs

        try:
            request = IntegrationRepairDispatchArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("stale", f"invalid repair dispatch: {exc}")
        _operation, authorized = await self._repair_command_authorized(
            request.operation_id, "integration_repair_dispatch"
        )
        if not authorized:
            return _failure("unauthorized", "repair dispatch authority is outside the operation")
        result = await self._integration_repair_service().dispatch(
            request.operation_id, request.stage
        )
        return {
            "success": result["outcome"]
            in {"dispatched", "already_dispatched", "writer_reused"},
            **result,
        }

    async def _cmd_integration_file_children(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationFileChildrenArgs
        from src.database.queries.hierarchy_queries import HierarchyError

        try:
            request = IntegrationFileChildrenArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid child filing: {exc}")
        parent = await self.db.get_task(request.parent_id)
        if parent is None:
            return _failure("invalid", "parent task not found")
        if not await self._integration_delivery_authorized(
            parent.project_id, "integration_file_children"
        ):
            return _failure("unauthorized", "caller cannot file integration children")
        try:
            result = await self._hierarchy_integration_service().file_children(
                request.parent_id, request.children, request.expected_generation
            )
        except HierarchyError as exc:
            outcome = exc.code if exc.code in {"stale_parent", "invalid"} else "invalid"
            return _failure(outcome, str(exc))
        except GitError as exc:
            return _failure("runtime_error", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_checkpoint_parent(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationCheckpointParentArgs
        from src.database.queries.hierarchy_queries import HierarchyError

        try:
            request = IntegrationCheckpointParentArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("dirty", f"invalid parent checkpoint: {exc}")
        task = await self.db.get_task(request.task_id)
        if task is None:
            return _failure("stale", "task not found")
        if not await self._integration_delivery_authorized(
            task.project_id, "integration_checkpoint_parent"
        ):
            return _failure("unauthorized", "caller cannot checkpoint this parent")
        try:
            result = await self._hierarchy_integration_service().checkpoint_parent(
                request.task_id, request.head_sha, request.generation
            )
        except HierarchyError as exc:
            outcome = exc.code if exc.code in {"dirty", "stale"} else "stale"
            return _failure(outcome, str(exc))
        except GitError as exc:
            return _failure("runtime_error", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_mutate_hierarchy(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationMutateHierarchyArgs
        from src.database.queries.hierarchy_queries import HierarchyError

        try:
            request = IntegrationMutateHierarchyArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid hierarchy mutation: {exc}")
        task = await self.db.get_task(request.task_id)
        if task is None:
            return _failure("invalid", "task not found")
        if not await self._integration_delivery_authorized(
            task.project_id, "integration_mutate_hierarchy"
        ):
            return _failure("unauthorized", "caller cannot mutate this hierarchy")
        try:
            result = await self._hierarchy_integration_service().mutate_hierarchy(
                request.task_id, request.mutation, request.arguments
            )
        except HierarchyError as exc:
            outcome = exc.code if exc.code in {
                "sealed",
                "delivery_target_fixed",
                "reopen_required",
                "invalid",
            } else "invalid"
            return _failure(outcome, str(exc))
        return {"success": True, **result}

    async def _cmd_integration_delivery_readiness(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationDeliveryReadinessArgs
        from src.database.queries.hierarchy_queries import HierarchyError

        try:
            request = IntegrationDeliveryReadinessArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invariant_error", f"invalid readiness query: {exc}")
        task = await self.db.get_task(request.task_id)
        if task is None:
            return _failure("invariant_error", "parent task not found")
        if not await self._integration_delivery_authorized(
            task.project_id, "integration_delivery_readiness", allow_session_read=True
        ):
            return _failure("unauthorized", "caller cannot read parent delivery state")
        try:
            result = await self._hierarchy_integration_service().readiness(request.task_id)
        except HierarchyError as exc:
            return _failure("invariant_error", str(exc))
        return {"success": result["outcome"] == "ready", **result}

    async def _cmd_integration_parent_verify(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationParentVerifyArgs
        from src.database.queries.hierarchy_queries import HierarchyError

        try:
            request = IntegrationParentVerifyArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid_evidence", f"invalid verification request: {exc}")
        task = await self.db.get_task(request.task_id)
        if task is None:
            return _failure("stale_generation", "parent task not found")
        if not await self._integration_delivery_authorized(
            task.project_id, "integration_parent_verify"
        ):
            return _failure("unauthorized", "caller cannot verify this parent")
        try:
            result = await self._hierarchy_integration_service().verify_parent(
                request.task_id,
                request.generation,
                request.head_sha,
                request.evidence_ids,
            )
        except (HierarchyError, GitError) as exc:
            return _failure("invalid_evidence", str(exc))
        return {"success": result["outcome"] == "verified", **result}

    async def _cmd_integration_complete_parent(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationCompleteParentArgs
        from src.database.queries.hierarchy_queries import HierarchyError

        try:
            request = IntegrationCompleteParentArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invariant_error", f"invalid parent completion: {exc}")
        task = await self.db.get_task(request.task_id)
        if task is None:
            return _failure("invariant_error", "parent task not found")
        if not await self._integration_delivery_authorized(
            task.project_id, "integration_complete_parent"
        ):
            return _failure("unauthorized", "caller cannot complete this parent")
        try:
            result = await self._hierarchy_integration_service().complete_parent(
                request.task_id, request.generation, request.head_sha
            )
        except HierarchyError as exc:
            return _failure("invariant_error", str(exc))
        return {"success": result["outcome"] == "completed", **result}

    async def _cmd_delivery_promote(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import DeliveryPromoteArgs
        from src.integration.models import PromotionInput
        from src.integration.ownership import BranchBusy, StaleFence
        from src.integration.promotion import (
            PromotionConflict,
            PromotionInvariantError,
            PromotionRuntimeError,
            PromotionSourceMoved,
            PromotionTargetMoved,
        )

        try:
            parsed = DeliveryPromoteArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("source_moved", f"invalid promotion request: {exc}")
        task = await self.db.get_task(parsed.source_task_id)
        repository = await self.db.get_repo(parsed.fence.target.repository_id)
        if (
            task is None
            or repository is None
            or task.project_id != repository.project_id
            or task.repo_id != repository.id
        ):
            return _failure("source_moved", "source task and repository identity do not match")
        if not await self._integration_delivery_authorized(task.project_id, "delivery_promote"):
            return _failure("unauthorized", "caller cannot promote delivery for this project")

        request = PromotionInput.model_validate(parsed.model_dump(mode="json"))
        service = self._integration_promotion_service()
        try:
            prepared = await service.prepare(request)
            existing = await self.db.get_integration_promotion_intent(prepared.intent_id)
            if existing is not None and existing["state"] == "committed":
                return self._promotion_result("already_promoted", prepared)
            owner = await BranchOwnership(self.db).get_owner(request.fence.target)
            if (
                owner is None
                or owner["owner_id"] != request.fence.owner_id
                or int(owner["fence_token"]) != request.fence.token
                or owner["owner_role"] != "collector"
                or owner["handoff_state"] != "reserved"
                or not await self._integration_collector_matches_target(
                    owner["owner_id"], request.fence.target, repository.project_id
                )
            ):
                return _failure(
                    "target_moved",
                    "actual promotion requires the current persisted collector owner",
                )
            promoted = await service.push(prepared.intent_id, request.fence)
        except PromotionConflict as exc:
            return self._promotion_result("conflict", exc.value, success=False, error=str(exc))
        except PromotionSourceMoved as exc:
            return _failure("source_moved", str(exc))
        except (PromotionTargetMoved, StaleFence, BranchBusy) as exc:
            return _failure("target_moved", str(exc))
        except PromotionInvariantError as exc:
            return _failure("runtime_error", str(exc))
        except (PromotionRuntimeError, GitError) as exc:
            return _failure("runtime_error", str(exc))
        return self._promotion_result("promoted", promoted)

    async def _cmd_integration_reconcile_promotion(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationReconcilePromotionArgs
        from src.integration.promotion import (
            PromotionConflict,
            PromotionInvariantError,
            PromotionNotApplied,
            PromotionRuntimeError,
            PromotionTargetMoved,
        )

        try:
            parsed = IntegrationReconcilePromotionArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invariant_error", f"invalid promotion reconciliation: {exc}")
        intent = await self.db.get_integration_promotion_intent(parsed.intent_id)
        if intent is None or not intent.get("project_id"):
            return _failure("invariant_error", "promotion intent does not exist")
        if not await self._integration_delivery_authorized(
            intent["project_id"], "integration_reconcile_promotion"
        ):
            return _failure("unauthorized", "caller cannot reconcile this project")
        try:
            value = await self._integration_promotion_service().reconcile(parsed.intent_id)
        except PromotionNotApplied as exc:
            return _failure("not_applied", str(exc))
        except (PromotionConflict, PromotionInvariantError, PromotionTargetMoved) as exc:
            return _failure("invariant_error", str(exc))
        except (PromotionRuntimeError, GitError) as exc:
            return _failure("runtime_error", str(exc))
        return self._promotion_result("applied", value)

    async def _cmd_integration_resolve_conflict(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationResolveConflictArgs
        from src.integration.models import ConflictResolutionInput
        from src.integration.ownership import BranchBusy, StaleFence
        from src.integration.promotion import (
            PromotionAuthorizationError,
            PromotionInvariantError,
            PromotionTargetMoved,
        )

        try:
            parsed = IntegrationResolveConflictArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invariant_error", f"invalid conflict resolution: {exc}")
        try:
            value, replay = await self._integration_promotion_service().reserve_resolution(
                ConflictResolutionInput.model_validate(parsed.model_dump(mode="json"))
            )
        except PromotionAuthorizationError as exc:
            return _failure("unauthorized", str(exc))
        except (PromotionTargetMoved, StaleFence, BranchBusy) as exc:
            return _failure("stale", str(exc))
        except (PromotionInvariantError, ValueError) as exc:
            return _failure("invariant_error", str(exc))
        return self._promotion_result("already_reserved" if replay else "reserved", value)

    async def _cmd_integration_push_conflict_resolution(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationPushConflictResolutionArgs
        from src.integration.ownership import BranchBusy, StaleFence
        from src.integration.promotion import (
            PromotionAuthorizationError,
            PromotionInvariantError,
            PromotionRuntimeError,
            PromotionSourceMoved,
            PromotionTargetMoved,
        )

        try:
            parsed = IntegrationPushConflictResolutionArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid resolution push: {exc}")
        try:
            value, replay = await self._integration_promotion_service().push_resolution(
                parsed.intent_id, parsed.fence
            )
        except PromotionAuthorizationError as exc:
            return _failure("unauthorized", str(exc))
        except (StaleFence, BranchBusy) as exc:
            return _failure("stale", str(exc))
        except PromotionTargetMoved as exc:
            outcome = "stale" if "authority is stale" in str(exc) else "target_moved"
            return _failure(outcome, str(exc))
        except (PromotionInvariantError, PromotionSourceMoved, PromotionRuntimeError) as exc:
            return _failure("runtime_error", str(exc))
        return self._promotion_result("already_applied" if replay else "pushed", value)

    async def _cmd_delivery_receipts(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import DeliveryReceiptsArgs

        try:
            parsed = DeliveryReceiptsArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid receipt query: {exc}")
        task = await self.db.get_task(parsed.source_task_id)
        repository = await self.db.get_repo(parsed.repository_id)
        if (
            task is None
            or repository is None
            or task.project_id != repository.project_id
            or task.repo_id != repository.id
        ):
            return _failure("unauthorized", "receipt query is outside the source project")
        if not await self._integration_delivery_authorized(
            task.project_id, "delivery_receipts", allow_session_read=True
        ):
            return _failure("unauthorized", "caller cannot read this project's receipts")
        receipts = await self.db.list_integration_delivery_receipts(
            source_task_id=parsed.source_task_id,
            repository_id=parsed.repository_id,
            target_branch=parsed.target_branch,
        )
        return {
            "success": True,
            "outcome": "found" if receipts else "not_found",
            "receipts": receipts,
        }

    @staticmethod
    def _promotion_result(outcome, value, *, success: bool = True, error: str | None = None):
        result = {
            "success": success,
            "outcome": outcome,
            **value.model_dump(mode="json"),
        }
        if error:
            result["error"] = error
        return result
