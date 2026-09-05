"""Command-handler namespace for hierarchical integration primitives.

Handlers are added here only with the task that implements their durable
mechanism.  An absent handler remains an explicit ``Unknown command`` refusal;
there are intentionally no optimistic success stubs.
"""

from __future__ import annotations

from typing import Any

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.git.manager import GitError
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
        if task.branch_name == target.branch:
            return True
        operation = await self.db.get_active_integration_repair_for_task(task_id)
        return bool(
            operation
            and await self._integration_operation_matches_target(operation, target, project_id)
        )

    async def _integration_destination_matches_target(
        self, owner_id: str, role: str, target: BranchKey, project_id: str
    ) -> bool:
        if role == "repair":
            return await self._integration_repair_task_matches_target(owner_id, target, project_id)
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
                return {
                    "success": True,
                    "outcome": "transferred",
                    "fence": fence.model_dump(mode="json"),
                }
            return _failure("stale_owner", "branch ownership fence is stale")

        fence = Fence(
            target=target,
            owner_id=current["owner_id"],
            token=current_token,
        )
        try:
            transferred = await ownership.transfer(fence, request.next_owner_id, request.next_role)
        except StaleFence as exc:
            return _failure("stale_owner", str(exc))
        except BranchBusy as exc:
            return _failure("busy", str(exc))
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
