"""Command-handler namespace for hierarchical integration primitives.

Handlers are added here only with the task that implements their durable
mechanism.  An absent handler remains an explicit ``Unknown command`` refusal;
there are intentionally no optimistic success stubs.
"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.commands.supervisor_authority import integration_operator
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
            _label, refusal = await integration_operator(self.db, repository.project_id)
            if refusal is not None:
                return _failure("human_required", refusal)
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

    def _integration_scheduler(self):
        scheduler = getattr(self.orchestrator, "integration_scheduler", None)
        if scheduler is not None:
            return scheduler
        from src.integration.scheduler import IntegrationScheduler

        return IntegrationScheduler(self.db)

    def _integration_control_service(self):
        service = getattr(self.orchestrator, "integration_control_service", None)
        if service is not None:
            return service
        from src.integration.controls import IntegrationControlService

        return IntegrationControlService(
            self.db,
            scheduler=self._integration_scheduler(),
            cleanup_service=getattr(self.orchestrator, "integration_cleanup_service", None),
            legacy_resolution_observer=(
                self._integration_promotion_service().observe_legacy_resolution_target
            ),
        )

    async def _integration_operator_for_operation(
        self, operation_id: str
    ) -> tuple[str | None, str | None]:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL and not (
            principal.kind is PrincipalKind.SESSION and principal.elevated
        ):
            return None, "a local operator or live supervisor session is required"
        operation = await self.db.get_integration_operation(operation_id)
        project_id = (
            await self._integration_operation_project_id(operation)
            if operation is not None
            else None
        )
        return await integration_operator(self.db, project_id)

    async def _integration_operator_for_batch(
        self, batch_id: str
    ) -> tuple[str | None, str | None]:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL and not (
            principal.kind is PrincipalKind.SESSION and principal.elevated
        ):
            return None, "a local operator or live supervisor session is required"
        batch = await self.db.get_integration_batch(batch_id)
        return await integration_operator(
            self.db, str(batch["project_id"]) if batch is not None else None
        )

    async def _cmd_integration_status(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        if not project_id:
            return _failure("not_found", "project_id is required")
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SESSION and principal.project_id is None:
            # The global supervisor has no project scope to match, so it is
            # admitted the way the controls admit it: as a live, elevated,
            # named supervisor session.  A projectless worker terminal is not.
            _label, refusal = await integration_operator(getattr(self, "db", None), project_id)
            authorized = refusal is None
        elif principal.kind is PrincipalKind.SESSION:
            authorized = principal.project_id == project_id
        elif principal.kind is PrincipalKind.PLAYBOOK:
            authorized = bool(
                not principal.unresolved
                and principal.project_id == project_id
                and principal.policy.allows("aq_commands", "integration_status")
            )
        else:
            authorized = principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}
        if not authorized:
            return _failure("unauthorized", "integration status is outside the caller project")
        return await self._integration_control_service().status(project_id)

    async def _cmd_integration_flush(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        if not project_id:
            return _failure("not_found", "project_id is required")
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.PLAYBOOK:
            authorized = await self._integration_delivery_authorized(project_id, "integration_flush")
        else:
            _label, refusal = await integration_operator(getattr(self, "db", None), project_id)
            authorized = refusal is None
        if not authorized:
            return _failure("unauthorized", "integration flush is outside the caller authority")
        project = await self.db.get_project(project_id)
        if getattr(project, "hierarchical_integration_mode", "disabled") == "development":
            return await self._development_integration().sweep(project_id)
        await self._reconcile_integration_completion(project_id)
        return await self._integration_control_service().flush(project_id)

    async def _cmd_integration_eject(self, args: dict) -> dict:
        batch_id = str(args.get("batch_id") or "")
        task_id = str(args.get("task_id") or "")
        reason = str(args.get("reason") or "")
        if not batch_id or not task_id or not reason.strip():
            return _failure("invalid_state", "batch_id, task_id and reason are required")
        operator_id, refusal = await self._integration_operator_for_batch(batch_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        return await self._integration_control_service().eject(
            batch_id, task_id=task_id, reason=reason, operator_id=operator_id
        )

    async def _reconcile_integration_completion(self, project_id: str) -> None:
        from src.integration.completion_recovery import (
            reconcile_closed_integration_owners, recover_completed_pr_links,
        )

        await reconcile_closed_integration_owners(self.orchestrator, project_id)
        recovered = await recover_completed_pr_links(
            self.db, self._integration_promotion_service(), project_id
        )
        if recovered:
            await self.db.log_event(
                "integration.pr_links_recovered", project_id=project_id,
                payload=json.dumps({"task_ids": recovered}),
            )


    async def _cmd_integration_enable(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        operator_id, refusal = await integration_operator(getattr(self, "db", None), project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            project_id = str(args["project_id"])
            mode = str(args["mode"])
            expected_generation = int(args["expected_generation"])
            reason = str(args["reason"])
            interval_seconds = args.get("interval_seconds")
        except (KeyError, TypeError, ValueError):
            return _failure("blocked", "project_id, mode, expected_generation, and reason are required")
        return await self._integration_control_service().enable(
            project_id,
            mode=mode,
            expected_generation=expected_generation,
            reason=reason,
            operator_id=operator_id,
            waiver_id=args.get("waiver_id"),
            interval_seconds=interval_seconds,
        )

    async def _cmd_integration_waive_history(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        operator_id, refusal = await integration_operator(getattr(self, "db", None), project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            return await self._integration_control_service().waive_history(
                str(args["project_id"]),
                reason=str(args["reason"]),
                blocker_digest=str(args["blocker_digest"]),
                operator_id=operator_id,
            )
        except KeyError:
            return _failure("not_waivable", "project_id, reason, and blocker_digest are required")

    async def _cmd_integration_reconcile_unmaterialized(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        operator_id, refusal = await integration_operator(getattr(self, "db", None), project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            project_id = str(args["project_id"])
            expected_generation = int(args["expected_generation"])
            reason = str(args["reason"])
        except (KeyError, TypeError, ValueError):
            return _failure("blocked", "project_id, expected_generation, and reason are required")
        try:
            return await self._integration_control_service().reconcile_unmaterialized_tasks(
                project_id,
                expected_generation=expected_generation,
                reason=reason,
                operator_id=operator_id,
                hierarchy=self._hierarchy_integration_service(),
            )
        except HierarchyError as exc:
            return _failure(f"hierarchy.{exc.code}", exc.detail)

    async def _cmd_integration_resume(self, args: dict) -> dict:
        operation_id = str(args.get("operation_id") or "")
        if not operation_id:
            return _failure("not_found", "operation_id is required")
        _label, refusal = await self._integration_operator_for_operation(operation_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        return await self._integration_control_service().resume(operation_id)

    async def _cmd_integration_abort(self, args: dict) -> dict:
        operation_id = str(args.get("operation_id") or "")
        reason = str(args.get("reason") or "")
        if not operation_id or not reason.strip():
            return _failure("invalid_state", "operation_id and reason are required")
        _label, refusal = await self._integration_operator_for_operation(operation_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        return await self._integration_control_service().abort(operation_id, reason=reason)

    async def _cmd_integration_retry_cleanup(self, args: dict) -> dict:
        batch_id = str(args.get("batch_id") or "")
        if not batch_id:
            return _failure("not_found", "batch_id is required")
        _label, refusal = await self._integration_operator_for_batch(batch_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        return await self._integration_control_service().retry_cleanup(batch_id)

    async def _cmd_integration_release_delegates(self, args: dict) -> dict:
        """Settle the delegates of one operation that already ended."""
        operation_id = str(args.get("operation_id") or "")
        if not operation_id:
            return _failure("not_found", "operation_id is required")
        _label, refusal = await self._integration_operator_for_operation(operation_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        return await self._integration_control_service().release_delegates(operation_id)

    async def _cmd_integration_recover_candidate_member(self, args: dict) -> dict:
        """Recover a durable pushed root-candidate repair."""
        from pydantic import ValidationError
        from sqlalchemy import select

        from src.commands.contracts.integration import IntegrationRecoverCandidateMemberArgs
        from src.database.tables import integration_candidate_resolutions
        from src.integration.candidates import CandidateAuthorizationError

        try:
            request = IntegrationRecoverCandidateMemberArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("stale", f"invalid candidate member recovery: {exc}")
        async with self.db._engine.connect() as conn:
            reservation = (
                await conn.execute(
                    select(integration_candidate_resolutions).where(
                        integration_candidate_resolutions.c.id == request.reservation_id
                    )
                )
            ).mappings().one_or_none()
        if reservation is None:
            return _failure("stale", "candidate repair reservation does not exist")
        batch = await self.db.get_integration_batch(reservation["batch_id"])
        if batch is None:
            return _failure("stale", "candidate repair batch does not exist")
        _label, refusal = await integration_operator(getattr(self, "db", None), str(batch["project_id"]))
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            result = await (await self._integration_candidate_service(batch)).recover_repair(
                request.reservation_id
            )
        except CandidateAuthorizationError as exc:
            return _failure("stale", str(exc))
        return {"success": result.outcome in {"accepted", "already_accepted", "rejected"},
                **result.model_dump(mode="json")}

    async def _cmd_integration_release_owner(self, args: dict) -> dict:
        """Run the fenced owner-recovery check for one owner or task."""
        from pydantic import ValidationError
        from sqlalchemy import select

        from src.commands.contracts.integration import IntegrationReleaseOwnerArgs
        from src.database.tables import integration_branch_owners
        from src.integration.owner_recovery import RECOVERABLE_STATES, owner_recovery_for

        try:
            request = IntegrationReleaseOwnerArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("not_eligible", f"invalid owner recovery request: {exc}")

        async with self.db._engine.connect() as conn:
            statement = select(integration_branch_owners)
            if request.owner_row_id is not None:
                statement = statement.where(integration_branch_owners.c.id == request.owner_row_id)
            else:
                statement = statement.where(
                    integration_branch_owners.c.owner_id == request.task_id,
                    integration_branch_owners.c.handoff_state.in_(RECOVERABLE_STATES),
                )
            rows = [dict(row) for row in (await conn.execute(statement)).mappings().all()]

        project_ids: set[str] = set()
        for row in rows:
            repository = await self.db.get_repo(row["repository_id"])
            if repository is not None:
                project_ids.add(repository.project_id)
        if not project_ids and request.task_id is not None:
            task = await self.db.get_task(request.task_id)
            if task is not None:
                project_ids.add(task.project_id)
        if len(project_ids) > 1:
            return _failure("unauthorized", "owner rows span multiple projects")
        project_id = next(iter(project_ids), None)
        principal, refusal = await integration_operator(self.db, project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)

        recovery = owner_recovery_for(self.orchestrator)
        if recovery is None:
            return _failure("runtime_error", "owner recovery is unavailable")
        owner_row_ids = [row["id"] for row in rows]
        if request.owner_row_id is not None and not owner_row_ids:
            owner_row_ids = [request.owner_row_id]
        outcomes = await recovery.recover_many(
            owner_row_ids, principal=principal, dry_run=request.dry_run
        )
        serialized = [outcome.to_dict() for outcome in outcomes]
        if not serialized:
            outcome = "not_found"
        elif any(item["outcome"] == "preserved_and_released" for item in serialized):
            outcome = "preserved_and_released"
        elif any(item["outcome"] == "released" for item in serialized):
            outcome = "released"
        elif any(item.get("reason") == "not_found" for item in serialized):
            outcome = "not_found"
        else:
            outcome = "not_eligible"
        return {"success": True, "outcome": outcome, "outcomes": serialized}

    async def _cmd_integration_reserve_owner(self, args: dict) -> dict:
        """Restore one stopped producer's missing canonical branch reservation."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationReserveOwnerArgs
        from src.integration.canonical_reservation import reserve_canonical_task_branch

        try:
            request = IntegrationReserveOwnerArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("not_eligible", f"invalid reservation request: {exc}")
        task = await self.db.get_task(request.task_id)
        if task is None:
            return _failure("not_found", "task does not exist")
        _principal, refusal = await integration_operator(self.db, task.project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        result = await reserve_canonical_task_branch(self.db, task.id)
        return {"success": result["outcome"] in {"acquired", "already_reserved"}, **result}

    async def _cmd_integration_release_stale_owners(self, args: dict) -> dict:
        """Release a project's provably safe reserved owners; report every other."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationReleaseStaleOwnersArgs
        from src.commands.provider_commands import parse_duration
        from src.integration.stale_owners import stale_owner_release_for

        try:
            request = IntegrationReleaseStaleOwnersArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid stale owner release request: {exc}")
        principal, refusal = await integration_operator(self.db, request.project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        service = stale_owner_release_for(self.orchestrator)
        if service is None:
            return _failure("runtime_error", "stale owner release is unavailable")
        result = await service.run(
            request.project_id,
            principal=principal,
            dry_run=request.dry_run,
            older_than_seconds=(
                parse_duration(request.older_than) if request.older_than is not None else None
            ),
        )
        return {"success": result["outcome"] != "not_found", **result}

    async def _cmd_integration_clear_stale_request(self, args: dict) -> dict:
        """Classify a project's outstanding sweep request; release it if it can never end."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationClearStaleRequestArgs

        try:
            request = IntegrationClearStaleRequestArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid stale request release: {exc}")
        operator_id, refusal = await integration_operator(self.db, request.project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        result = await self._integration_control_service().clear_stale_request(
            request.project_id,
            dry_run=request.dry_run,
            expected_request_id=request.expected_request_id,
            reason=request.reason,
            operator_id=operator_id,
        )
        return {
            "success": result["outcome"] in {"cleared", "would_clear", "nothing_to_clear"},
            **result,
        }

    async def _cmd_integration_redrive_root(self, args: dict) -> dict:
        """Diagnose a completed train root's missing PR; open it for the reported head."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRedriveRootArgs
        from src.integration.root_pull_requests import RootDeliveryRedrive

        try:
            request = IntegrationRedriveRootArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid root redrive request: {exc}")
        task = await self.db.get_task(request.task_id)
        principal, refusal = await integration_operator(
            self.db, task.project_id if task is not None else None
        )
        if refusal is not None:
            return _failure("unauthorized", refusal)
        result = await RootDeliveryRedrive(self.db, self.orchestrator.git).run(
            request.task_id,
            dry_run=request.dry_run,
            expected_head_sha=request.expected_head_sha,
            reason=request.reason,
            operator_id=principal,
        )
        return {
            "success": result["outcome"] in {"would_open", "opened", "nothing_to_redrive"},
            "dry_run": request.dry_run,
            **result,
        }

    async def _cmd_integration_redrive_child(self, args: dict) -> dict:
        """Diagnose a completed child its parent never assembled; advance it for the head."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRedriveChildArgs
        from src.integration.child_delivery import ChildDelivery

        try:
            request = IntegrationRedriveChildArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid child redrive request: {exc}")
        task = await self.db.get_task(request.task_id)
        principal, refusal = await integration_operator(
            self.db, task.project_id if task is not None else None
        )
        if refusal is not None:
            return _failure("unauthorized", refusal)
        collection = getattr(self.orchestrator, "integration_collection", None)
        collect = None
        if collection is not None:
            async def collect(parent_id: str) -> str | None:
                return await collection.collect_parent(parent_id, time.time())

        result = await ChildDelivery(
            self.db, self._integration_promotion_service(), collect=collect
        ).run(
            request.task_id,
            dry_run=request.dry_run,
            expected_head_sha=request.expected_head_sha,
            reason=request.reason,
            operator_id=principal,
        )
        return {
            "success": result["outcome"] in {"would_advance", "advanced", "nothing_to_redrive"},
            "dry_run": request.dry_run,
            **result,
        }

    async def _cmd_integration_rebind_reused_identity(self, args: dict) -> dict:
        """Prove a task's inherited integration identity; rebind it once settled."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRebindReusedIdentityArgs
        from src.integration.identity_rebind import reused_identity_rebind_for

        try:
            request = IntegrationRebindReusedIdentityArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid identity rebind request: {exc}")
        task = await self.db.get_task(request.task_id)
        if task is None:
            return _failure("not_found", f"task {request.task_id} not found")
        principal, refusal = await integration_operator(self.db, task.project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        service = reused_identity_rebind_for(self)
        if service is None:
            return _failure("runtime_error", "identity rebind is unavailable")
        result = await service.run(
            request.task_id,
            principal=principal,
            dry_run=request.dry_run,
            expected_origin_ids=request.expected_origin_ids,
            discard_tips=request.discard_tips,
            reason=request.reason,
        )
        return {
            "success": result["outcome"] in {"rebound", "would_rebind", "nothing_to_rebind"},
            **result,
        }

    async def _cmd_integration_rebind_repair(self, args: dict) -> dict:
        """Prove a live repair candidate and reserve it under the current intent."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRebindRepairArgs
        from src.integration.promotion import PromotionError
        from src.integration.repair_rebind import RepairRebind

        try:
            request = IntegrationRebindRepairArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("blocked", f"invalid repair rebind request: {exc}")
        task = await self.db.get_task(request.task_id)
        if task is None:
            return _failure("not_found", f"task {request.task_id} not found")
        _principal, refusal = await integration_operator(self.db, task.project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            result = await RepairRebind(self._integration_promotion_service()).run(
                request.task_id,
                dry_run=request.dry_run,
                expected_head_sha=request.expected_head_sha,
            )
        except (PromotionError, GitError, ValueError) as exc:
            return _failure("blocked", str(exc))
        return {
            "success": result["outcome"] in {"would_rebind", "rebound", "already_reserved"},
            **result,
        }

    async def _cmd_integration_adopt_legacy_deliveries(self, args: dict) -> dict:
        """Record provable pre-train deliveries of children no train will collect."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationAdoptLegacyDeliveriesArgs
        from src.integration.legacy_deliveries import legacy_delivery_adoption_for

        try:
            request = IntegrationAdoptLegacyDeliveriesArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid legacy delivery adoption request: {exc}")
        principal, refusal = await integration_operator(self.db, request.project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        service = legacy_delivery_adoption_for(self)
        if service is None:
            return _failure("runtime_error", "legacy delivery adoption is unavailable")
        result = await service.run(
            request.project_id,
            principal=principal,
            dry_run=request.dry_run,
            accept=request.accept,
            retire=request.retire,
            supersede=request.supersede,
            reason=request.reason,
        )
        return {"success": result["outcome"] in {"adopted", "nothing_to_adopt"}, **result}

    async def _cmd_integration_bind_legacy_repositories(self, args: dict) -> dict:
        """Bind terminal hierarchy members with designated-repository delivery proof."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationBindLegacyRepositoriesArgs
        from src.integration.legacy_repositories import LegacyRepositoryBinding

        try:
            request = IntegrationBindLegacyRepositoriesArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid legacy repository binding request: {exc}")
        principal, refusal = await integration_operator(self.db, request.project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        result = await LegacyRepositoryBinding(self.db).run(
            request.project_id, principal=principal, dry_run=request.dry_run,
            reason=request.reason,
        )
        return {"success": result["outcome"] in {"bound", "nothing_to_bind"}, **result}

    def _integration_train_service(self):
        service = getattr(self.orchestrator, "integration_train_service", None)
        if service is not None:
            return service
        from src.integration.scheduler import TrainService

        return TrainService(
            self.db,
            default_mode=self.config.integration.default_mode,
        )

    def _integration_root_promotion_service(self):
        service = getattr(self.orchestrator, "root_promotion_service", None)
        if service is not None:
            return service
        from src.integration.main_promotion import RootPromotionService

        return RootPromotionService(
            self.db,
            data_dir=self.config.data_dir,
            git_manager=self.orchestrator.git,
            app_client=getattr(self.orchestrator, "integration_app_client", None),
            github_client_factory=getattr(
                self.orchestrator, "github_client_factory", None
            ),
            attestation_resolver=getattr(
                self.orchestrator, "integration_attestation_resolver", None
            ),
        )

    def _integration_cleanup_service(self):
        service = getattr(self.orchestrator, "integration_cleanup_service", None)
        if service is not None:
            return service
        from src.integration.cleanup import IntegrationCleanupService

        return IntegrationCleanupService(
            self.db,
            data_dir=self.config.data_dir,
            git_manager=self.orchestrator.git,
            github_client_factory=getattr(
                self.orchestrator, "github_client_factory", None
            ),
            forge_provider=getattr(
                self.orchestrator, "integration_cleanup_forge_provider", None
            ),
        )

    async def _integration_candidate_service(
        self, batch: dict[str, Any], *, repair_session=None
    ):
        service = getattr(self.orchestrator, "integration_candidate_service", None)
        if service is not None:
            return service
        from src.integration.candidates import CandidateService

        app_client = None
        binding_resolver = getattr(
            self.orchestrator, "github_repository_binding_resolver", None
        )
        github_client_factory = getattr(
            self.orchestrator, "github_client_factory", None
        )
        repository = await self.db.get_repo(batch["repository_id"])
        if repository is not None and binding_resolver is not None and github_client_factory is not None:
            binding = binding_resolver(repository)
            if inspect.isawaitable(binding):
                binding = await binding
            if binding is not None:
                app_client = github_client_factory(binding)
                if inspect.isawaitable(app_client):
                    app_client = await app_client
        branch_ownership = None
        if repair_session is not None:
            callback_name = (
                "aconfirm_integration_pool_owner_handoff"
                if repair_session.lifecycle == "pool"
                else "aconfirm_integration_owner_stopped_for_repair"
            )
            branch_ownership = BranchOwnership(
                self.db,
                confirm_handoff=getattr(self.orchestrator, callback_name, None),
            )
        return CandidateService(
            self.db,
            data_dir=self.config.data_dir,
            git_manager=self.orchestrator.git,
            app_client=app_client,
            forge_provider=app_client,
            repair_service=self._integration_repair_service(),
            branch_ownership=branch_ownership,
        )

    def _integration_release_service(self):
        service = getattr(self.orchestrator, "integration_release_service", None)
        if service is not None:
            return service
        from src.integration.release import IntegrationReleaseService

        return IntegrationReleaseService(self.db)

    async def _cmd_integration_schedule_due(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationScheduleDueArgs

        try:
            request = IntegrationScheduleDueArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid integration schedule trigger: {exc}")
        project = await self.db.get_project(request.project_id)
        if project is None:
            return _failure("runtime_error", "integration schedule project does not exist")
        if not await self._integration_delivery_authorized(
            request.project_id, "integration_schedule_due"
        ):
            return _failure("unauthorized", "caller cannot schedule this project")
        result = await self._integration_scheduler().mark_due(
            request.project_id, request.now, request.trigger
        )
        return {
            "success": result["outcome"] in {"due", "not_due", "coalesced"},
            **result,
        }

    async def _cmd_integration_seal(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationSealArgs

        try:
            request = IntegrationSealArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid integration seal request: {exc}")
        project = await self.db.get_project(request.project_id)
        if project is None:
            return _failure("runtime_error", "integration seal project does not exist")
        if not await self._integration_delivery_authorized(
            request.project_id, "integration_seal"
        ):
            return _failure("unauthorized", "caller cannot seal this project")
        result = await self._integration_train_service().seal(
            request.project_id,
            request.request_id,
            request.now if request.now is not None else time.time(),
        )
        return {
            "success": result["outcome"] in {"sealed", "empty"},
            **result,
        }

    async def _cmd_integration_promote_main(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationPromoteMainArgs
        from src.integration.main_promotion import RootPromotionInvariantError

        try:
            request = IntegrationPromoteMainArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid root promotion request: {exc}")
        batch = await self.db.get_integration_batch(request.batch_id)
        if batch is None:
            return _failure("runtime_error", "integration batch does not exist")
        if not await self._integration_delivery_authorized(
            batch["project_id"], "integration_promote_main"
        ):
            return _failure("unauthorized", "caller cannot promote this root batch")
        try:
            result = await self._integration_root_promotion_service().promote(
                request.batch_id, request.revision
            )
        except RootPromotionInvariantError as exc:
            return _failure("runtime_error", str(exc))
        return {
            "success": result.outcome in {"promoted", "already_promoted"},
            **result.model_dump(mode="json"),
        }

    async def _cmd_integration_cleanup(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationCleanupArgs

        try:
            request = IntegrationCleanupArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid integration cleanup request: {exc}")
        batch = await self.db.get_integration_batch(request.batch_id)
        if batch is None:
            return _failure("stale", "integration batch does not exist")
        if not await self._integration_delivery_authorized(
            batch["project_id"], "integration_cleanup"
        ):
            return _failure("unauthorized", "caller cannot clean up this root batch")
        service = self._integration_cleanup_service()
        materialized = await service.materialize(request.batch_id)
        if materialized.outcome not in {"materialized", "already_materialized"}:
            return {
                "success": False,
                **materialized.model_dump(mode="json"),
            }
        advanced = await service.advance(request.batch_id)
        from sqlalchemy import func, select

        from src.database.tables import integration_cleanup_items

        async with self.db._engine.connect() as conn:
            counts = dict(
                (
                    await conn.execute(
                        select(
                            integration_cleanup_items.c.state,
                            func.count().label("count"),
                        )
                        .where(
                            integration_cleanup_items.c.batch_id == request.batch_id
                        )
                        .group_by(integration_cleanup_items.c.state)
                    )
                ).all()
            )
        completed = int(counts.get("complete", 0))
        conflicts = int(counts.get("conflict", 0)) + int(counts.get("failed", 0))
        total = sum(int(value) for value in counts.values())
        if total != materialized.item_count:
            outcome = "invariant_error"
        elif conflicts:
            outcome = "conflict"
        elif completed == materialized.item_count:
            outcome = "complete" if advanced or materialized.item_count == 0 else "already_complete"
        elif counts.get("retryable", 0):
            outcome = "retryable"
        elif advanced:
            outcome = "advanced"
        else:
            outcome = "wait"
        return {
            "success": outcome in {"advanced", "complete", "already_complete"},
            "outcome": outcome,
            "batch_id": request.batch_id,
            "item_count": materialized.item_count,
            "completed_count": completed,
            "conflict_count": conflicts,
        }

    async def _cmd_integration_build_candidate(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationBuildCandidateArgs

        try:
            request = IntegrationBuildCandidateArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid candidate build request: {exc}")
        batch = await self.db.get_integration_batch(request.batch_id)
        if batch is None:
            return _failure("runtime_error", "integration batch does not exist")
        if not await self._integration_delivery_authorized(
            batch["project_id"], "integration_build_candidate"
        ):
            return _failure("unauthorized", "caller cannot build this root batch")
        if (
            request.expected_revision is not None
            and batch["current_revision"] != request.expected_revision
        ):
            return _failure("stale_revision", "candidate revision changed before build")
        service = await self._integration_candidate_service(batch)
        moved_main = False
        if batch["lifecycle"] == "building":
            from sqlalchemy import select

            from src.database.tables import integration_promotion_intents

            async with self.db._engine.connect() as conn:
                moved_main = (
                    await conn.execute(
                        select(integration_promotion_intents.c.id).where(
                            integration_promotion_intents.c.intent_kind == "root",
                            integration_promotion_intents.c.root_batch_id == request.batch_id,
                            integration_promotion_intents.c.root_candidate_revision
                            == batch["current_revision"],
                            integration_promotion_intents.c.state == "superseded",
                        )
                    )
                ).scalar_one_or_none() is not None
        if moved_main and batch["policy_snapshot"].get("on_main_moved", "rebuild") != "rebuild":
            return {
                "success": False,
                "outcome": "base_moved",
                "batch_id": request.batch_id,
                "revision": int(batch["current_revision"]),
            }
        if moved_main and getattr(service, "app_client", None) is not None:
            repository = await service._repository(batch["repository_id"])
            new_base = await service.app_client.exact_head_ref(repository.default_branch)
            result = (
                await service.rebuild(
                    request.batch_id, int(batch["current_revision"]), new_base
                )
                if new_base is not None
                else await service.build(request.batch_id)
            )
        else:
            result = await service.build(request.batch_id)
        if (
            result.outcome == "base_moved"
            and batch["policy_snapshot"].get("on_main_moved", "rebuild") == "rebuild"
            and getattr(service, "app_client", None) is not None
        ):
            repository = await service._repository(batch["repository_id"])
            new_base = await service.app_client.exact_head_ref(repository.default_branch)
            if new_base is not None:
                result = await service.rebuild(
                    request.batch_id, int(batch["current_revision"]), new_base
                )
        if (
            request.expected_revision is not None
            and result.revision != request.expected_revision
        ):
            return _failure("stale_revision", "candidate revision changed during build")
        return {
            "success": result.outcome in {"empty", "built", "already_built"},
            **result.model_dump(mode="json"),
        }

    async def _cmd_integration_repair_close_current(self, args: dict) -> dict:
        """Resolve a closed root writer only while its adopted revision is current."""
        from pydantic import ValidationError
        from sqlalchemy import select

        from src.commands.contracts.integration import IntegrationRepairCloseCurrentArgs
        from src.database.tables import (
            integration_candidate_revisions,
            integration_outbox,
            integration_repair_stages,
        )

        try:
            request = IntegrationRepairCloseCurrentArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid repair close request: {exc}")
        operation, authorized = await self._repair_command_authorized(
            request.operation_id, "integration_repair_close_current"
        )
        if not authorized:
            return _failure("unauthorized", "caller cannot resolve this repair close")
        if operation is None:
            return _failure("stale", "repair operation does not exist")
        event_id = (
            f"repair-delegate-closed-{request.operation_id}-{request.stage}-{request.task_id}"
            f"-{request.fence_token}-{request.session_id}"
        )
        async with self.db._engine.connect() as conn:
            event = (await conn.execute(
                select(integration_outbox).where(
                    integration_outbox.c.id == event_id,
                    integration_outbox.c.event_type == "integration.repair_delegate_closed",
                )
            )).mappings().one_or_none()
            expected = request.model_dump(mode="json")
            if event is None or any(
                event["payload"].get(key) != value for key, value in expected.items()
            ):
                return _failure("stale", "repair close event does not match its fence")
            if operation["target_kind"] != "batch":
                return {"success": True, "outcome": "not_batch"}
            batch = await self.db.get_integration_batch(operation["batch_id"])
            if batch is None or event["project_id"] != batch["project_id"]:
                return _failure("stale", "root batch does not match repair close")
            stage = (await conn.execute(
                select(integration_repair_stages).where(
                    integration_repair_stages.c.operation_id == request.operation_id,
                    integration_repair_stages.c.ordinal == request.stage,
                )
            )).mappings().one_or_none()
            revision = event["payload"].get("revision")
            candidate = (await conn.execute(
                select(integration_candidate_revisions).where(
                    integration_candidate_revisions.c.batch_id == batch["id"],
                    integration_candidate_revisions.c.revision == revision,
                )
            )).mappings().one_or_none() if isinstance(revision, int) else None
        delegate = await self.db.get_task(request.task_id)
        subject = stage["current_subject"] if stage is not None else None
        if (
            operation["state"] not in {"active", "escalated"}
            or operation["active_stage"] != request.stage
            or stage is None
            or stage["state"] not in {"active", "awaiting_completion"}
            or stage["writer_kind"] != "repair_delegate"
            or stage["repair_task_id"] != request.task_id
            or delegate is None
            or delegate.status is not TaskStatus.COMPLETED
            or event["payload"].get("batch_id") != batch["id"]
            or revision != batch["current_revision"]
            or candidate is None
            or candidate["head_sha"] != event["payload"].get("head_sha")
            or subject != {
                "kind": "batch", "revision": revision,
                "candidate_sha": candidate["head_sha"],
            }
        ):
            return _failure("stale", "repair close is no longer the current candidate")
        return {
            "success": True,
            "outcome": "current",
            "batch_id": batch["id"],
            "revision": revision,
        }

    async def _cmd_integration_ci_evidence(self, args: dict) -> dict:
        from pydantic import ValidationError
        from sqlalchemy import select

        from src.commands.contracts.integration import IntegrationCIEvidenceArgs
        from src.database.tables import (
            integration_batches,
            integration_candidate_revisions,
            integration_repair_operations,
        )

        try:
            request = IntegrationCIEvidenceArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid candidate CI request: {exc}")
        batch = await self.db.get_integration_batch(request.batch_id)
        if batch is None:
            return _failure("runtime_error", "integration batch does not exist")
        if not await self._integration_delivery_authorized(
            batch["project_id"], "integration_ci_evidence"
        ):
            return _failure("unauthorized", "caller cannot observe this root candidate")
        async with self.db._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        integration_repair_operations.c.id.label("operation_id"),
                        integration_candidate_revisions.c.batch_id,
                        integration_candidate_revisions.c.revision,
                        integration_candidate_revisions.c.head_sha.label("candidate_sha"),
                    )
                    .select_from(
                        integration_candidate_revisions.join(
                            integration_repair_operations,
                            integration_repair_operations.c.batch_id
                            == integration_candidate_revisions.c.batch_id,
                        ).join(
                            integration_batches,
                            integration_batches.c.id
                            == integration_candidate_revisions.c.batch_id,
                        )
                    )
                    .where(
                        integration_candidate_revisions.c.batch_id == request.batch_id,
                        integration_candidate_revisions.c.revision == request.revision,
                        integration_batches.c.id == request.batch_id,
                        integration_batches.c.current_revision == request.revision,
                    )
                )
            ).mappings().one_or_none()
        if row is None or row["candidate_sha"] is None:
            return _failure("stale_subject", "candidate revision is not current and built")
        service = getattr(self.orchestrator, "integration_attestation_service", None)
        if service is None:
            return _failure("configuration_blocked", "trusted CI adapter is unavailable")
        result = await service.handle_candidate_ci(dict(row), 0.0)
        outcome = result.get("outcome")
        public_outcome = "pending" if outcome == "not_green" else outcome
        return {
            "success": outcome in {"green", "published", "already_published"},
            "outcome": (
                "green"
                if outcome in {"published", "already_published"}
                else public_outcome
            ),
            "batch_id": request.batch_id,
            "revision": request.revision,
            "evidence_ids": tuple(result.get("evidence_ids") or ()),
            "aggregate_evidence_id": result.get("aggregate_evidence_id"),
        }

    async def _cmd_integration_release(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationReleaseArgs

        try:
            request = IntegrationReleaseArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid integration release request: {exc}")
        batch = await self.db.get_integration_batch(request.batch_id)
        if batch is None:
            return _failure("stale", "integration batch does not exist")
        if not await self._integration_delivery_authorized(
            batch["project_id"], "integration_release"
        ):
            return _failure("unauthorized", "caller cannot release this root batch")
        result = await self._integration_release_service().release(
            request.batch_id, time.time()
        )
        return {
            "success": result.outcome in {"released", "already_released", "empty"},
            **result.model_dump(mode="json"),
        }

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
                    promotion.git, str(resolved.retained_git_dir), branch, base_sha,
                    repository_url=resolved.origin_url,
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
            git_manager=self.orchestrator.git,
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
            from src.database.queries.task_identity import resolve_task_identity_on

            async with self.db._engine.connect() as conn:
                identity = await resolve_task_identity_on(
                    conn, operation.get("parent_task_id") or ""
                )
            return identity.project_id if identity is not None else None
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
        operation, authorized = await self._repair_command_authorized(
            request.operation_id, "integration_repair_dispatch"
        )
        if not authorized or operation is None:
            return _failure("unauthorized", "repair dispatch authority is outside the operation")
        if request.batch_id is not None:
            from sqlalchemy import select

            from src.database.tables import integration_candidate_revisions

            batch = await self.db.get_integration_batch(request.batch_id)
            async with self.db._engine.connect() as conn:
                candidate = (
                    await conn.execute(
                        select(integration_candidate_revisions).where(
                            integration_candidate_revisions.c.batch_id
                            == request.batch_id,
                            integration_candidate_revisions.c.revision
                            == request.revision,
                        )
                    )
                ).mappings().one_or_none()
            if (
                operation.get("target_kind") != "batch"
                or operation.get("batch_id") != request.batch_id
                or batch is None
                or int(batch["current_revision"]) != request.revision
                or candidate is None
                or candidate["head_sha"] != request.head_sha
            ):
                return _failure("stale", "candidate repair subject is no longer current")
        stage = int(operation["active_stage"]) if request.stage is None else request.stage
        result = await self._integration_repair_service().dispatch(
            request.operation_id, stage
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

    async def _cmd_integration_record_noop(self, args: dict) -> dict:
        from pydantic import ValidationError
        from sqlalchemy import select

        from src.commands.contracts.integration import IntegrationRecordNoopArgs
        from src.database.tables import integration_review_evidence
        from src.integration.promotion import PromotionError, PromotionSourceMoved

        try:
            request = IntegrationRecordNoopArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid no-op disposition: {exc}")
        child = await self.db.get_task(request.child_task_id)
        if child is None or not child.parent_task_id or not child.repo_id:
            return _failure("invalid", "disposition child not found")
        if not await self._integration_delivery_authorized(
            child.project_id, "integration_record_noop"
        ):
            return _failure("unauthorized", "caller cannot dispose this child")
        checkpoint = await self.db.get_integration_checkpoint(child.id)
        if checkpoint is None or checkpoint["checkpoint_sha"] != request.expected_head_sha:
            return _failure("stale_head", "child checkpoint is not the expected head")
        completion = await self.db.get_task_completion(child.id)
        if completion is None or completion.outcome != "pass" or completion.work_outcome != "no-op":
            return _failure("invalid", "child has no current no-op completion")
        review_id = None
        if child.profile_id in {"reviewer", "final-reviewer"}:
            async with self.db._engine.connect() as conn:
                review_id = (
                    await conn.execute(
                        select(integration_review_evidence.c.id)
                        .where(
                            integration_review_evidence.c.reviewer_task_id == child.id,
                            integration_review_evidence.c.verdict == "approved",
                        )
                        .order_by(
                            integration_review_evidence.c.created_at.desc(),
                            integration_review_evidence.c.id.desc(),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if review_id is None:
                return _failure("invalid", "approved reviewer evidence is missing")
        try:
            promotion = self._integration_promotion_service()
            resolved = await promotion._resolve_repository(child.repo_id)
            if resolved.repo.project_id != child.project_id:
                return _failure("invalid", "child repository project changed")
            await promotion._ensure_retained_repository(resolved)
            async with promotion.git.arepository_transaction(str(resolved.retained_git_dir)):
                await promotion._fetch_all_heads(resolved.retained_git_dir, resolved.origin_url)
                tree = await promotion._tree_oid(resolved.retained_git_dir, request.expected_head_sha)
            principal = current_principal() or TRUSTED_LOCAL
            receipt = await self._hierarchy_integration_service().record_disposition(
                child.id,
                disposition="noop",
                reviewed_head_sha=request.expected_head_sha,
                reviewed_tree_sha=tree,
                verification_evidence={
                    "kind": "task_noop_completion",
                    "completion_id": completion.id,
                    "review_evidence_id": review_id,
                },
                resolution_evidence={
                    "authority": principal.kind.value,
                    "completion_id": completion.id,
                    "review_evidence_id": review_id,
                },
                verified_completion_id=completion.id,
                verified_review_id=review_id,
            )
        except HierarchyError as exc:
            outcome = "delivery_target_fixed" if exc.code == "delivery_target_fixed" else "invalid"
            return _failure(outcome, str(exc))
        except PromotionSourceMoved as exc:
            return _failure("stale_head", str(exc))
        except PromotionError as exc:
            return _failure("runtime_error", str(exc))
        except GitError as exc:
            return _failure("runtime_error", str(exc))
        return {
            "success": True,
            "outcome": "recorded",
            "receipt_id": receipt["id"],
            "revision": receipt["revision"],
            "reviewed_head_sha": receipt["reviewed_head_sha"],
            "reviewed_tree_sha": receipt["reviewed_tree_sha"],
        }

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
        # ``already_completed`` is the crash-retry replay of a durable completion
        # for this exact operation, generation, head and verification, so it is a
        # success like every other ``already_*`` outcome in this module.
        return {
            "success": result["outcome"] in {"completed", "already_completed"},
            **result,
        }

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
            PromotionSourceMoved,
            PromotionRuntimeError,
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
        except (PromotionInvariantError, PromotionSourceMoved, ValueError) as exc:
            return _failure("invariant_error", str(exc))
        except (PromotionRuntimeError, GitError) as exc:
            return _failure("runtime_error", str(exc))
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

    async def _cmd_integration_resolve_candidate_member(self, args: dict) -> dict:
        """Resolve only the candidate conflict assigned to the authenticated writer.

        Candidate identity and every authority-bearing value are derived from
        the current session attachment.  The request carries only Git object
        evidence plus the pool claim fence; in particular it cannot select a
        batch/member/operation or supply trusted lineage/a branch fence.
        """
        from dataclasses import replace

        from pydantic import ValidationError
        from sqlalchemy import select

        from src.commands.contracts.integration import IntegrationResolveCandidateMemberArgs
        from src.commands.principal import ExecutionPrincipal, principal_context
        from src.database.tables import (
            integration_candidate_member_results,
            integration_candidate_resolutions,
            integration_candidate_revisions,
        )
        from src.integration.candidates import (
            CandidateAuthorizationError,
            CandidateResolutionInput,
            CandidateStaleAuthority,
        )

        try:
            request = IntegrationResolveCandidateMemberArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invariant_error", f"invalid candidate member repair: {exc}")

        principal = current_principal()
        if (
            principal is None
            or principal.kind is not PrincipalKind.SESSION
            or principal.session_id is None
            or principal.session_instance_token is None
        ):
            return _failure(
                "unauthorized", "candidate member repair requires an authenticated session"
            )
        session = await self.db.get_session(principal.session_id)
        task_id = getattr(session, "task_id", None) if session is not None else None
        if (
            session is None
            or task_id is None
            or session.state not in {"starting", "running", "draining"}
            or session.instance_token != principal.session_instance_token
            or request.task_id not in (None, task_id)
            or request.session_id not in (None, principal.session_id)
            or request.project_id not in (None, session.project_id)
            or principal.task_id not in (None, task_id)
            or principal.project_id not in (None, session.project_id)
        ):
            return _failure("unauthorized", "candidate repair session identity is stale")

        claim_error = await self._assert_session_owns(
            task_id,
            session_id=principal.session_id,
            claim_epoch=request.claim_epoch,
        )
        if claim_error is not None:
            outcome = "stale" if claim_error.get("result") == "stale_claim" else "unauthorized"
            return _failure(outcome, str(claim_error.get("error") or "claim is stale"))
        task = await self.db.get_task(task_id)
        if (
            task is None
            or (session.lifecycle == "pool" and session.last_claim_epoch is None)
            or (
                session.last_claim_epoch is not None
                and int(session.last_claim_epoch) != int(task.claim_epoch)
            )
        ):
            return _failure("stale", "candidate repair claim is no longer current")

        exact_principal: ExecutionPrincipal = replace(principal, task_id=task_id)
        exact_values = {
            "repair_task_id": task_id,
            "repair_session_id": principal.session_id,
            "repair_session_instance_token": principal.session_instance_token,
            "resolved_head_sha": request.resolved_head_sha,
            "resolved_tree_sha": request.resolved_tree_sha,
        }
        async with self.db._engine.connect() as conn:
            exact_rows = (
                await conn.execute(
                    select(integration_candidate_resolutions).where(
                        *(
                            integration_candidate_resolutions.c[key] == value
                            for key, value in exact_values.items()
                        )
                    )
                )
            ).mappings().all()
        exact_rows = [
            dict(row)
            for row in exact_rows
            if tuple(row["repair_commit_shas"]) == request.repair_commit_shas
            and (
                session.lifecycle != "pool"
                or session.claim_phase_at is not None
                and float(row["created_at"]) >= float(session.claim_phase_at)
            )
        ]
        if len(exact_rows) > 1:
            return _failure("invariant_error", "candidate repair identity is ambiguous")

        scope = await self.db.get_repair_filing_scope(
            task_id, session_id=principal.session_id
        )
        reservation = exact_rows[0] if exact_rows else None
        if reservation is not None and reservation["state"] in {"pushed", "accepted"}:
            batch = await self.db.get_integration_batch(reservation["batch_id"])
            if batch is None:
                return _failure("stale", "candidate repair batch is absent")
            service = await self._integration_candidate_service(batch, repair_session=session)
            try:
                with principal_context(exact_principal):
                    accepted = await service.accept_repair(reservation["id"])
                continuation = None
                if accepted.outcome in {"accepted", "already_accepted"}:
                    continuation = await service.build(batch["id"])
            except CandidateAuthorizationError as exc:
                return _failure("unauthorized", str(exc))
            except (CandidateStaleAuthority, StaleFence, BranchBusy) as exc:
                return _failure("stale", str(exc))
            except ValueError as exc:
                return _failure("invariant_error", str(exc))
            except (GitError, RuntimeError) as exc:
                return _failure("runtime_error", str(exc))
            return {
                "success": accepted.outcome in {"accepted", "already_accepted"},
                "outcome": accepted.outcome,
                "reservation_id": reservation["id"],
                "batch_id": reservation["batch_id"],
                "revision": int(reservation["revision"]),
                "member_ordinal": int(reservation["member_ordinal"]),
                "partial_head_sha": reservation["partial_head_sha"],
                "continuation": (
                    continuation.model_dump(mode="json")
                    if continuation is not None
                    else None
                ),
            }

        if (
            scope is None
            or not scope["active"]
            or scope["writer_kind"] != "repair_delegate"
            or scope["target_kind"] != "batch"
            or scope["fence_token"] is None
        ):
            return _failure("unauthorized", "candidate repair writer authority is stale")

        operation = await self.db.get_integration_operation(scope["operation_id"])
        batch = (
            await self.db.get_integration_batch(operation["batch_id"])
            if operation is not None and operation.get("batch_id")
            else None
        )
        if batch is None or batch["project_id"] != session.project_id:
            return _failure("unauthorized", "candidate repair batch is outside the session")

        async with self.db._engine.connect() as conn:
            revision = (
                await conn.execute(
                    select(integration_candidate_revisions).where(
                        integration_candidate_revisions.c.batch_id == batch["id"],
                        integration_candidate_revisions.c.revision
                        == batch["current_revision"],
                    )
                )
            ).mappings().one_or_none()
            member = None
            if revision is not None:
                member = (
                    await conn.execute(
                        select(integration_candidate_member_results).where(
                            integration_candidate_member_results.c.batch_id == batch["id"],
                            integration_candidate_member_results.c.revision
                            == revision["revision"],
                            integration_candidate_member_results.c.member_ordinal
                            == revision["next_member_ordinal"],
                        )
                    )
                ).mappings().one_or_none()
        evidence = member["conflict_evidence"] if member is not None else None
        if (
            revision is None
            or member is None
            or member["result"] != "conflict"
            or not evidence
            or evidence.get("operation_id") != scope["operation_id"]
            or int(evidence.get("revision", -1)) != int(revision["revision"])
            or int(evidence.get("ordinal", -1)) != int(member["member_ordinal"])
        ):
            return _failure("stale", "the assigned candidate member is no longer conflicted")

        fence = Fence(
            target=BranchKey(
                repository_id=batch["repository_id"], branch=batch["integration_branch"]
            ),
            owner_id=task_id,
            token=int(scope["fence_token"]),
        )
        candidate_request = CandidateResolutionInput(
            batch_id=batch["id"],
            revision=int(revision["revision"]),
            member_ordinal=int(member["member_ordinal"]),
            operation_id=scope["operation_id"],
            resolved_head_sha=request.resolved_head_sha,
            resolved_tree_sha=request.resolved_tree_sha,
            repair_commit_shas=request.repair_commit_shas,
            fence=fence,
        )
        service = await self._integration_candidate_service(batch, repair_session=session)
        try:
            with principal_context(exact_principal):
                reservation_id = await service.reserve_repair(candidate_request)
                await service.push_repair(reservation_id, fence)
                accepted = await service.accept_repair(reservation_id)
            continuation = None
            if accepted.outcome in {"accepted", "already_accepted"}:
                continuation = await service.build(batch["id"])
        except CandidateAuthorizationError as exc:
            return _failure("unauthorized", str(exc))
        except (CandidateStaleAuthority, StaleFence, BranchBusy) as exc:
            return _failure("stale", str(exc))
        except ValueError as exc:
            return _failure("invariant_error", str(exc))
        except (GitError, RuntimeError) as exc:
            return _failure("runtime_error", str(exc))

        return {
            "success": accepted.outcome in {"accepted", "already_accepted"},
            "outcome": accepted.outcome,
            "reservation_id": reservation_id,
            "batch_id": accepted.batch_id,
            "revision": accepted.revision,
            "member_ordinal": accepted.member_ordinal,
            "partial_head_sha": evidence["partial_head_sha"],
            "continuation": (
                continuation.model_dump(mode="json") if continuation is not None else None
            ),
        }

    async def _cmd_integration_recover_unwritten_resolution(self, args: dict) -> dict:
        """Operator-only recovery for a malformed reservation with no write attempt."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRecoverUnwrittenResolutionArgs
        from src.integration.ownership import BranchBusy, StaleFence
        from src.integration.promotion import (
            PromotionAuthorizationError,
            PromotionInvariantError,
            PromotionRuntimeError,
            PromotionTargetMoved,
        )

        try:
            parsed = IntegrationRecoverUnwrittenResolutionArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("not_recoverable", f"invalid resolution recovery: {exc}")
        intent = await self.db.get_integration_promotion_intent(parsed.intent_id)
        project_id = intent.get("project_id") if intent is not None else None
        _label, refusal = await integration_operator(self.db, project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            value, replay = await self._integration_promotion_service().recover_unwritten_resolution(
                parsed.intent_id
            )
        except (PromotionAuthorizationError, PromotionInvariantError, PromotionTargetMoved, StaleFence, BranchBusy) as exc:
            return _failure("not_recoverable", str(exc))
        except (PromotionRuntimeError, GitError) as exc:
            return _failure("runtime_error", str(exc))
        return self._promotion_result("already_recovered" if replay else "recovered", value)

    async def _cmd_delivery_receipts(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import DeliveryReceiptsArgs

        try:
            parsed = DeliveryReceiptsArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid receipt query: {exc}")
        from src.database.queries.task_identity import resolve_task_identity_on

        async with self.db._engine.connect() as conn:
            task = await resolve_task_identity_on(conn, parsed.source_task_id)
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


    def _development_integration(self):
        from src.integration.development import DevelopmentIntegration
        service = getattr(self.orchestrator, "development_integration", None)
        if service is not None:
            return service
        return DevelopmentIntegration(self.db, data_dir=self.config.data_dir,
                                      git=self.orchestrator.git)

    async def _cmd_integration_develop(self, args: dict) -> dict:
        operator_id, refusal = await integration_operator(
            getattr(self, "db", None), str(args.get("project_id") or "")
        )
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            return await self._development_integration().configure(
                args["project_id"], args["policy"], reason=args["reason"], operator_id=operator_id)
        except (ValueError, RuntimeError, KeyError) as exc:
            return _failure("blocked", str(exc))

    async def _cmd_integration_adopt(self, args: dict) -> dict:
        operator_id, refusal = await integration_operator(
            getattr(self, "db", None), str(args.get("project_id") or "")
        )
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            return await self._development_integration().adopt(
                project_id=args["project_id"], task_ids=args["task_ids"],
                target_ref=args["target_ref"], head_sha=args["head_sha"], reason=args["reason"],
                operator_id=operator_id, accept_equivalent=args.get("accept_equivalent", False))
        except (ValueError, RuntimeError, KeyError) as exc:
            return _failure("blocked", str(exc))

    async def _cmd_integration_development_sweep(self, args: dict) -> dict:
        _label, refusal = await integration_operator(
            getattr(self, "db", None), str(args.get("project_id") or "")
        )
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            service = self._development_integration()
            if args.get("recover_child"):
                return await service.recover_child(
                    args["project_id"], args["recover_child"], retry=args.get("retry", False)
                )
            return await service.sweep(args["project_id"], retry=args.get("retry", False))
        except (ValueError, RuntimeError, KeyError) as exc:
            return _failure("blocked", str(exc).strip() or repr(exc))

    async def _cmd_integration_cancel_preserving(self, args: dict) -> dict:
        operation_id = str(args.get("operation_id") or "")
        _label, refusal = await self._integration_operator_for_operation(operation_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            return await self._development_integration().cancel_preserving(args["operation_id"], reason=args["reason"])
        except (ValueError, RuntimeError, KeyError) as exc:
            return _failure("blocked", str(exc))
