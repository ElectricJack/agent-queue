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

from src.integration.reconciler_repair_close import reconciler_batch_subject
from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.commands.supervisor_authority import integration_operator
from src.git.manager import GitError
from src.git.manager import RemoteRefState
from src.database.queries.hierarchy_queries import HierarchyError
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchBusy, BranchOwnership, StaleFence
from src.integration.owner_guards import parent_engine_guard
from src.models import TaskStatus


_TASK_OWNER_ROLES = frozenset({"worker", "repair", "verifier"})

#: Stored origin of a source-CI repair.  The router reads it as the policy's
#: ``integration_repair`` origin; the delegate claim, restart and release
#: paths that key on a stored ``integration_repair`` never see it, because a
#: source-CI repair is a root with its own branch, not a stage delegate.
SOURCE_CI_REPAIR_ORIGIN = "source_ci_repair"


def _failure(outcome: str, error: str) -> dict[str, Any]:
    return {"success": False, "outcome": outcome, "error": error}


#: Root-train subject commands an elevated live supervisor may re-drive.
#: Sealing, scheduling and parent-delivery writers stay service/playbook-only.
_SUPERVISOR_REDRIVE_CAPABILITIES = frozenset({
    "integration_build_candidate",
    "integration_ci_evidence",
    "integration_cleanup",
    "integration_promote_main",
    "integration_release",
    "integration_repair_close_current",
})


def _with_reason(success: bool, payload: dict[str, Any]) -> dict[str, Any]:
    """Surface a service refusal's ``reason`` as the envelope ``error``.

    The reason is not a contract result field, so the reviewed playbooks'
    command fingerprints do not change; adapters carry it as the summary.
    """
    reason = payload.pop("reason", None)
    result = {"success": success, **payload}
    if reason and not success:
        result["error"] = reason
    return result


class IntegrationCommandsMixin:
    async def _cmd_integration_retire_legacy_park(self, args: dict) -> dict:
        from src.commands.contracts.integration import IntegrationRetireLegacyParkArgs
        from src.integration.legacy_park_retirement import LegacyParkRetirement

        principal = current_principal()
        if principal is None or principal.kind is not PrincipalKind.LOCAL:
            return _failure("refused", "historical park retirement requires the local operator")
        request = IntegrationRetireLegacyParkArgs.model_validate(args)
        try:
            result = await LegacyParkRetirement(self.db).run(request)
        except (ValueError, HierarchyError, BranchBusy, TimeoutError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_reconcile_expired_mutation(self, args: dict) -> dict:
        from src.commands.contracts.integration import IntegrationReconcileExpiredMutationArgs
        from src.integration.mutation_recovery import ExpiredMutationRecovery

        principal = current_principal()
        if principal is None or principal.kind is not PrincipalKind.LOCAL:
            return _failure("refused", "expired mutation recovery requires the local operator")
        request = IntegrationReconcileExpiredMutationArgs.model_validate(args)
        try:
            result = await ExpiredMutationRecovery(self.db).run(request)
        except (ValueError, GitError, HierarchyError, BranchBusy, TimeoutError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}


    async def _cmd_integration_cutover_plan(self, args: dict) -> dict:
        return await self._integration_cutover(args, read_only=True)

    async def _cmd_integration_cutover(self, args: dict) -> dict:
        return await self._integration_cutover(args, read_only=False)

    async def _integration_cutover(self, args, *, read_only):
        from src.commands.contracts.integration import IntegrationCutoverArgs
        from src.integration.cutover import Cutover, CutoverGit
        from src.git.github_contracts import GitHubAccessError
        from src.integration.protection import LocalRulesetReader

        request = IntegrationCutoverArgs.model_validate(args)
        if not read_only and (current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL:
            return _failure("unauthorized", "cutover and reverse require a local operator")
        operator, refusal = await integration_operator(self.db, request.project_id)
        if refusal:
            return _failure("unauthorized", refusal)
        project = await self.db.get_project(request.project_id)
        repository = await self.db.get_repo(project.integration_repository_id or "") if project else None
        if repository is None or repository.project_id != request.project_id:
            return _failure("refused", "project has no designated integration repository")
        try:
            resolver = getattr(self.orchestrator, "github_repository_binding_resolver", None)
            factory = getattr(self.orchestrator, "github_client_factory", None)
            if resolver is None or factory is None:
                return _failure("refused", "GitHub repository access is unavailable")
            binding = resolver(repository)
            if inspect.isawaitable(binding):
                binding = await binding
            if binding is None:
                return _failure("refused", "repository identity cannot be verified")
            client = factory(binding)
            if inspect.isawaitable(client):
                client = await client
            admin_reader = None
            if (current_principal() or TRUSTED_LOCAL).kind is PrincipalKind.LOCAL:
                admin_reader = LocalRulesetReader(self._promotion_user_client(binding))
            service = Cutover(self.db, CutoverGit(
                self.db, client, ruleset_admin_reader=admin_reader,
            ))
            options = {"allow_epics": request.allow_epics, "reverse": request.reverse}
            if read_only:
                result = {"outcome": "planned", "plan": await service.plan(
                    request.project_id, request.flow, **options)}
            else:
                result = await service.run(
                    request.project_id, request.flow, dry_run=request.dry_run,
                    expected_generation=request.expected_generation, operator_id=operator,
                    baseline=request.baseline, **options)
        except (ValueError, GitError, GitHubAccessError, OSError) as exc:
            return _failure("refused", str(exc))
        return {"success": result["outcome"] in {"planned", "preview", "configured"}, **result}

    async def _cmd_integration_quiesce(self, args: dict) -> dict:
        from src.commands.contracts.integration import IntegrationQuiesceArgs
        from src.integration.quiesce import TrainQuiesce

        principal = current_principal()
        if principal is None or principal.kind is not PrincipalKind.LOCAL:
            return _failure("refused", "quiesce requires the local operator")
        request = IntegrationQuiesceArgs.model_validate(args)
        try:
            result = await TrainQuiesce(self.db).run(request)
        except (ValueError, HierarchyError, BranchBusy, TimeoutError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_record_delivered(self, args: dict) -> dict:
        from src.commands.contracts.integration import IntegrationRecordDeliveredArgs
        from src.integration.delivered_close import DeliveredClose

        principal = current_principal()
        if principal is None or principal.kind is not PrincipalKind.LOCAL:
            return _failure("refused", "record-delivered requires the local operator")
        request = IntegrationRecordDeliveredArgs.model_validate(args)
        try:
            result = await DeliveredClose(self.db).record(request)
        except (ValueError, GitError, HierarchyError, BranchBusy, TimeoutError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_record_root_noop(self, args: dict) -> dict:
        from src.commands.contracts.integration import IntegrationRecordRootNoopArgs
        from src.integration.train_controls import TrainControls

        request = IntegrationRecordRootNoopArgs.model_validate(args)
        task = await self.db.get_task(request.task_id)
        operator, refusal = await integration_operator(self.db, task.project_id if task else None)
        if refusal:
            return _failure("unauthorized", refusal)
        try:
            result = await TrainControls(self.db).record_root_noop(
                request.task_id, dry_run=request.dry_run,
                expected_head_sha=request.expected_head_sha, operator_id=operator,
                reason=request.reason, expected_project_id=task.project_id if task else None,
            )
        except (ValueError, GitError, HierarchyError, BranchBusy, TimeoutError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}


    async def _cmd_integration_pause_batch(self, args: dict) -> dict:
        return await self._integration_batch_intent(args, "paused")

    async def _cmd_integration_resume_batch(self, args: dict) -> dict:
        return await self._integration_batch_intent(args, "open")

    async def _integration_batch_intent(self, args, intent):
        from src.commands.contracts.integration import IntegrationAbortBatchArgs
        from src.integration.batches import BatchStore
        from src.integration.train_controls import TrainControls

        request = IntegrationAbortBatchArgs.model_validate(args)
        batch = await BatchStore(self.db).get(request.batch_id)
        operator, refusal = await integration_operator(self.db, batch.project_id if batch else None)
        if refusal:
            return _failure("unauthorized", refusal)
        try:
            result = await TrainControls(self.db).set_batch_intent(
                request.batch_id, intent, dry_run=request.dry_run,
                operator_id=operator, reason=request.reason,
            )
        except (ValueError, GitError, OSError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_seal_now(self, args: dict) -> dict:
        from src.commands.contracts.integration import IntegrationSealNowArgs
        from src.integration.train_controls import TrainControls

        request = IntegrationSealNowArgs.model_validate(args)
        _operator, refusal = await integration_operator(self.db, request.project_id)
        if refusal:
            return _failure("unauthorized", refusal)
        try:
            result = await TrainControls(self.db).seal_now(request.project_id,
                train=getattr(self.orchestrator, "integration_train", None),
                dry_run=request.dry_run)
        except (ValueError, GitError, OSError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_refresh_epic(self, args: dict) -> dict:
        """Preview or refresh an epic from the default branch through the train."""
        from src.commands.contracts.integration import IntegrationRefreshEpicArgs
        from src.integration.train_controls import TrainControls

        request = IntegrationRefreshEpicArgs.model_validate(args)
        task = await self.db.get_task(request.task_id)
        operator, refusal = await integration_operator(self.db, task.project_id if task else None)
        if refusal:
            return _failure("unauthorized", refusal)
        try:
            result = await TrainControls(self.db, train=getattr(
                self.orchestrator, "integration_train", None)).refresh_epic(
                request.task_id, dry_run=request.dry_run, operator_id=operator)
        except (ValueError, GitError, OSError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_promotion_publish(self, args: dict) -> dict:
        """Mechanism entry for one admitted promotion; no worker can publish a target."""
        from sqlalchemy import select

        from src.commands.contracts.integration import IntegrationPromotionPublishArgs
        from src.database.tables import integration_batches
        from src.integration.batches import BatchStore
        from src.integration.train import TrainTarget

        request = IntegrationPromotionPublishArgs.model_validate(args)
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == request.batch_id,
            ))).mappings().one_or_none()
        if row is None:
            return _failure("not_found", "promotion batch does not exist")
        principal = current_principal()
        if principal is None or principal.kind not in {PrincipalKind.SERVICE, PrincipalKind.PLAYBOOK}:
            return _failure("unauthorized", "promotion publication requires a daemon service or reviewed playbook")
        if principal.kind is PrincipalKind.PLAYBOOK and (
            principal.unresolved or principal.project_id != row["project_id"]
        ):
            return _failure("unauthorized", "promotion publication is outside the playbook project")
        snapshot = row["policy_snapshot"] or {}
        if (row["trigger"] != "promotion" or not isinstance(snapshot, dict)
                or not isinstance(snapshot.get("promotion_step"), dict)
                or not (row["target_ref"] or "").startswith("refs/heads/")):
            return _failure("promotion_intent_invalid", "batch is not a frozen promotion intent")
        train = getattr(self.orchestrator, "integration_train", None)
        if train is None:
            return _failure("unavailable", "the integration train is not active")
        target = TrainTarget(row["project_id"], row["repository_id"], row["target_ref"],
                             "promotion", step=snapshot["promotion_step"])
        store = BatchStore(self.db)
        batch = await store.get(row["id"])
        if batch is None:
            return _failure("promotion_intent_invalid", "promotion batch has no frozen target")
        lane = await train.lane_for(target)
        observation = await lane.service.visit(batch, await store.members(batch.id),
                                               await lane.snapshot())
        if observation.state == "delivered":
            await train.batches.settle(batch, observation)
        return {"success": True, "outcome": observation.state, "batch_id": batch.id,
                "source_sha": observation.candidate_sha, "target_sha": observation.target_sha,
                "detail": observation.detail}

    async def _cmd_integration_abort_batch(self, args: dict) -> dict:
        """Preview or abort an unpromoted Git-first batch."""
        from src.commands.contracts.integration import IntegrationAbortBatchArgs
        from src.integration.batches import BatchStore
        from src.integration.train_controls import TrainControls

        request = IntegrationAbortBatchArgs.model_validate(args)
        batch = await BatchStore(self.db).get(request.batch_id)
        operator, refusal = await integration_operator(self.db, batch.project_id if batch else None)
        if refusal:
            return _failure("unauthorized", refusal)
        try:
            result = await TrainControls(self.db).abort_batch(
                request.batch_id, dry_run=request.dry_run, operator_id=operator,
                reason=request.reason,
            )
        except (ValueError, GitError, OSError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_retire_origin(self, args: dict) -> dict:
        """Retire only the previewed origin of a proven delivered completion."""
        from src.commands.contracts.integration import IntegrationRetireOriginArgs
        from src.integration.train_controls import TrainControls

        request = IntegrationRetireOriginArgs.model_validate(args)
        task = await self.db.get_task(request.task_id)
        operator, refusal = await integration_operator(self.db, task.project_id if task else None)
        if refusal:
            return _failure("unauthorized", refusal)
        try:
            result = await TrainControls(self.db).retire_origin(
                request.task_id, dry_run=request.dry_run, origin_id=request.origin_id,
                operator_id=operator, reason=request.reason,
            )
        except (ValueError, GitError, OSError) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_release_held_gate(self, args: dict) -> dict:
        """A human may lift a hold without changing its immutable gate answer."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationReleaseHeldGateArgs
        from src.integration.engine import EngineRefused
        from src.integration.gates import GatePrimitives
        from src.integration.runtime_contracts import Subject

        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            return _failure("unauthorized", "a verified human operator is required")
        try:
            request = IntegrationReleaseHeldGateArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("refused", str(exc))
        if not request.dry_run and (
            request.expected_version is None or not request.reason.strip()
        ):
            return _failure("refused", "release requires an exact subject version and reason")
        row = await self.db.get_integration_subject(request.subject_id)
        if row is None:
            return _failure("refused", "integration subject is missing")
        if not request.dry_run and row["version"] != request.expected_version:
            return _failure("refused", "stale_subject")
        try:
            result = await GatePrimitives(self.db).release_hold(
                Subject.from_row(row), request.gate_id, reason=request.reason,
                operator_id="human:local-operator", verified_human=True, dry_run=request.dry_run,
            )
        except EngineRefused as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def reverify_integration_parent_source(self, observation) -> dict:
        """Daemon-only adapter for a configured canonical PR head observation."""
        from src.integration.cancelled_collection_recovery import _ProofFailed
        from src.integration.parent_source import ParentHeadObservation, ParentSourceReverification

        principal = current_principal()
        if (principal is None or principal.kind is not PrincipalKind.SERVICE
                or not isinstance(observation, ParentHeadObservation)):
            return _failure("unauthorized", "parent head must be server-observed")
        try:
            result = await ParentSourceReverification(
                self.db, self._integration_repository_git(),
            ).run(observation.task_id, observation)
        except (HierarchyError, _ProofFailed, BranchBusy, StaleFence) as exc:
            return _failure("blocked", str(exc))
        return _with_reason(result["outcome"] == "reverifying", result)

    async def _cmd_integration_parent_action(self, args: dict) -> dict:
        """Internal visit dispatch: process-bound engine scope is the authority."""
        from src.integration.owner_guards import active_parent_scope
        from src.integration.runtime_contracts import Subject

        row = await self.db.get_integration_subject(args.get("subject_id"))
        if row is None or row["version"] != args.get("expected_version"):
            return _failure("stale", "parent subject version changed")
        subject = Subject.from_row(row)
        if not active_parent_scope(self.db, subject.task_id):
            return _failure("unauthorized", "parent action needs the active visit exclusion")
        runtime = getattr(self.orchestrator, "parent_subject_runtime", None)
        if runtime is None:
            return _failure("unavailable", "parent runtime unavailable")
        value = await runtime.adapters.perform(self, subject, args["request"])
        return {"success": not value.is_unknown, "value": value.model_dump(mode="json")}

    async def _cmd_integration_train_tick(self, args: dict) -> dict:
        """Start one level-triggered visit per idle target (git_first: active).

        Daemon-only: the integration service drives it under a service
        principal.  The visits run in the background, so a slow target never
        holds this command; it reports which targets started or still run.
        """
        principal = current_principal()
        if principal is None or principal.kind is not PrincipalKind.SERVICE:
            return _failure("unauthorized", "only the daemon may tick the integration train")
        train = getattr(self.orchestrator, "integration_train", None)
        if train is None:
            return _failure("unavailable", "the integration train is not active")
        return {"success": True, **await train.tick(args.get("now"))}

    async def _cmd_integration_engine_transfer(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationEngineTransferArgs
        from src.integration.engine import EngineRefused, RootEngineOwnership
        from src.integration.runtime_contracts import SubjectEngine

        try:
            request = IntegrationEngineTransferArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("refused", str(exc))
        repository = await self.db.get_repo(request.repository_id)
        if repository is None:
            return _failure("refused", "repository does not exist")
        operator, error = await integration_operator(self.db, repository.project_id)
        if error:
            return _failure("unauthorized", error)
        if (not request.dry_run and request.engine == "reconciler"
            and not self.config.integration.reconciler_active):
            return _failure("refused", "enable the active loop before transferring subjects")
        try:
            from src.integration.owner_guards import ParentEngineOwnership
            owner = (ParentEngineOwnership(self.db) if request.parent_task_id
                     else RootEngineOwnership(self.db))
            parent_args = {"task_id": request.parent_task_id} if request.parent_task_id else {}
            result = await owner.transfer(
                request.repository_id, engine=SubjectEngine(request.engine),
                expected_versions=request.expected_versions, reason=request.reason,
                evidence=request.evidence, operator_id=operator, dry_run=request.dry_run,
                **parent_args,
            )
        except (EngineRefused, BranchBusy, StaleFence) as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}

    async def _cmd_integration_development_engine_transfer(self, args: dict) -> dict:
        """Activate the project's subjects under the reconciler's exclusive fence."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import (
            IntegrationDevelopmentEngineTransferArgs,
        )
        from src.integration.development_runtime import transfer_development_engine

        try:
            request = IntegrationDevelopmentEngineTransferArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("refused", str(exc))
        if await self.db.get_project(request.project_id) is None:
            return _failure("refused", "project does not exist")
        operator, error = await integration_operator(self.db, request.project_id)
        if error:
            return _failure("unauthorized", error)
        if (not request.dry_run and request.engine == "reconciler"
            and not self.config.integration.reconciler_active):
            return _failure(
                "refused", "enable the active loop before transferring development subjects"
            )
        try:
            result = await transfer_development_engine(
                self.db,
                request.project_id,
                engine=request.engine,
                expected_versions=dict(request.expected_versions),
                reason=request.reason,
                evidence=tuple(request.evidence),
                operator_id=operator,
                dry_run=request.dry_run,
            )
        except ValueError as exc:
            return _failure("refused", str(exc))
        return {"success": True, **result}


    """Implemented integration command handlers are registered incrementally."""

    async def _cmd_observe_integration_source_ci(self, observation) -> dict:
        """Daemon-only adapter for trusted CI observations, never a raw tool input."""
        from sqlalchemy import text
        from src.integration.source_ci import SourceCIObservation
        if not isinstance(observation, SourceCIObservation):
            return _failure("invalid", "source observation must be server-observed")
        identity = self._integration_source_ci_identity(observation)
        # Serialize the complete observe/file/link sequence across daemons.
        # A restart releases this transaction lock; ensure_task recovers a
        # filed-but-not-yet-linked assignment by its immutable dedup key.
        async with self.db._engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                               {"key": identity})
            return await self._record_integration_source_ci(observation)

    @staticmethod
    def _integration_source_ci_identity(observation) -> str:
        import hashlib

        source = observation.source
        key = [source["project_id"], observation.task_id, source["repository_id"],
               source["base"], source["head"], source["generation"]]
        return "source-ci:" + hashlib.sha256(json.dumps(key).encode()).hexdigest()

    async def _record_integration_source_ci(self, observation) -> dict:
        from sqlalchemy import select, update
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from src.database.tables import (
            archived_tasks, integration_source_ci, projects, task_completion_records,
            task_metadata, tasks,
        )
        from src.integration.models import HierarchicalIntegrationPolicy
        from src.integration.review_evidence import ReviewEvidenceProducer
        from src.integration.source_ci import SourceCIObservation, repair_description
        from src.integration.source_repairs import RETIREMENT_KEY, prove_source_delivered

        if not isinstance(observation, SourceCIObservation):
            return _failure("invalid", "source observation must be server-observed")
        source = observation.source
        producer = ReviewEvidenceProducer(self.db, None)
        key = {
            "task_id": observation.task_id, "repository_id": source["repository_id"],
            "source_base": source["base"], "source_head": source["head"],
            "generation": source["generation"],
        }
        conditions = [integration_source_ci.c[name] == value for name, value in key.items()]
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, source["project_id"])
            project = (await conn.execute(select(projects).where(
                projects.c.id == source["project_id"]))).mappings().one()
            policy = HierarchicalIntegrationPolicy.model_validate(project["hierarchical_integration_policy"])
            if (not policy.root.repair.source_ci
                    or project["hierarchical_integration_generation"] != observation.policy_generation
                    or await producer._pull_request_source_on(conn, observation.task_id) != source
                    or not await producer._authorization_on(
                        conn, observation.task_id, source, observation.policy_generation, allow_reviewed=True)):
                return _failure("stale", "source policy, authorization or exact identity changed")
            values = {
                "policy_generation": observation.policy_generation,
                "state": observation.state, "evidence": observation.evidence,
                "observed_at": time.time(),
            }
            await conn.execute(pg_insert(integration_source_ci).values(**key, **values)
                .on_conflict_do_update(index_elements=list(key), set_=values))
            record = dict((await conn.execute(select(integration_source_ci)
                          .where(*conditions))).mappings().one())
            # Only a genuine failure is repairable code. Cancellation-only is
            # infrastructure: it waits and asks for a re-run of the exact head,
            # and it never files a repair — an agent handed it could only merge
            # the head again and hit the same outage, which is how one outage
            # became a chain of repairs of repairs. Every non-infra observation
            # resets the counter, so a head that recovers costs nothing later.
            infra = await self._record_source_ci_infrastructure(
                conn, conditions, record, observation, policy,
            )
            if observation.state != "red":
                return {
                    "success": True, "outcome": "observed", "state": observation.state,
                    "infra_observations": infra["infra_observations"],
                    # The caller owns the GitHub client, so the re-run is
                    # requested there rather than inside this transaction.
                    "rerun_requested": infra["rerun"],
                    **({"blocker": infra["blocker"]} if "blocker" in infra else {}),
                }
            existing_repair = record["repair_task_id"]
            delegate_open = False
            if existing_repair:
                status = (await conn.execute(select(tasks.c.status).where(
                    tasks.c.id == existing_repair))).scalar_one_or_none()
                if status is None:
                    status = (await conn.execute(select(archived_tasks.c.status).where(
                        archived_tasks.c.id == existing_repair))).scalar_one_or_none()
                retired = await conn.scalar(select(task_metadata.c.task_id).where(
                    task_metadata.c.task_id == existing_repair,
                    task_metadata.c.key == RETIREMENT_KEY,
                ))
                if retired is None:
                    # Completion audit survives archival of the obsolete task.
                    retired = await conn.scalar(select(task_completion_records.c.task_id).where(
                        task_completion_records.c.task_id == existing_repair,
                        task_completion_records.c.id.like("source-ci-retired:%"),
                    ).limit(1))
                if status != TaskStatus.FAILED.value and retired is None:
                    delegate_open = True
                elif status == TaskStatus.FAILED.value and policy.root.repair.on_exhausted != "continue":
                    return _failure("human_required", "source repair failed under finite policy")
            attempt = record["repair_attempt"] + (0 if delegate_open else 1)
        # Canonical delivery truth, asked of the same evidence the root
        # scheduler admits a delivered root on: the exact completion
        # generation, the exact repository and the exact default target.  It
        # runs outside the hierarchy lock because it is git I/O, and it runs
        # before filing so no agent is ever handed already-delivered work.
        #
        # Asked afresh on every observation and never reused from the record:
        # the target can be retargeted, the target can lose containment, and a
        # delivery or adoption can arrive after an earlier negative answer.  A
        # remembered answer answers a question nobody asked, so the record
        # below is audit only and no eligibility decision reads it back.
        proof = (await prove_source_delivered(
            self.db, getattr(self.db, "_delivery_observer", None),
            task_id=observation.task_id, source=source,
        ))
        recorded = await self._record_source_delivery(
            observation, source, proof, producer, conditions,
        )
        if recorded is not None:
            return recorded
        if delegate_open:
            # The delegate is not this handler's to end: a human gate, a finite
            # policy and a live writer all outrank a delivery answer.  It keeps
            # its writer, and the answer recorded above keeps the claim
            # frontier honest about it.
            return {"success": True, "outcome": "already_repairing",
                    "repair_task_id": existing_repair,
                    "delivery": proof.as_evidence()}
        if proof.delivered:
            return {
                "success": True, "outcome": "source_delivered",
                "state": observation.state, "task_id": observation.task_id,
                "source_head": source["head"], "generation": source["generation"],
                "delivery": proof.as_evidence(),
            }
        # Creation stays on the normal CommandHandler filing/routing path.
        # The enclosing source lock protects replay and competing ticks.
        created = await self._cmd_ensure_task({
            "project_id": source["project_id"], "repo_id": source["repository_id"],
            "dedup_key": f"{self._integration_source_ci_identity(observation)}:{attempt}",
            "title": f"Repair source CI: {observation.task_id} ({source['head'][:12]})",
            "description": repair_description(observation), "task_type": "bugfix",
            "root": True, "reason": "authorized exact source CI recovery",
            "intelligence_class": policy.root.repair.debug_intelligence_class,
            "_created_by_kind": SOURCE_CI_REPAIR_ORIGIN,
            "_created_by_id": self._integration_source_ci_identity(observation),
        })
        if not created.get("success"):
            return created
        retired = []
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, source["project_id"])
            current = (await conn.execute(select(integration_source_ci).where(*conditions)
                       .with_for_update())).mappings().one()
            if current["repair_attempt"] >= attempt:
                return {"success": True, "outcome": "already_repairing",
                        "repair_task_id": current["repair_task_id"]}
            history = list(current["repair_history"] or [])
            if current["repair_task_id"]:
                history.append({"task_id": current["repair_task_id"],
                                "attempt": current["repair_attempt"]})
            await conn.execute(update(integration_source_ci).where(*conditions).values(
                repair_task_id=created["task_id"], repair_attempt=attempt, repair_history=history))
            # Filing releases the hierarchy lock. A reopen during that gap
            # must also retire the newly linked delegate, never leave an
            # orphan writer able to complete the rejected source.
            if await producer._pull_request_source_on(conn, observation.task_id) != source:
                from src.integration.source_repairs import retire_reopened_source_repairs_on

                retired = await retire_reopened_source_repairs_on(
                    self.db, conn, observation.task_id, context="source_changed_during_repair_filing",
                    source_head=source["head"],
                )
        for transition in retired:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        if retired:
            return _failure("source_ci_repair_superseded", "source reopened during repair filing")
        return {
                "success": True, "outcome": "repair_created",
                "repair_task_id": created["task_id"],
                "delivery": proof.as_evidence(),
            }

    async def _record_source_ci_infrastructure(
        self, conn, conditions, record, observation, policy
    ) -> dict:
        """Count one infrastructure-only observation and ask for a bounded re-run.

        Returns whether a re-run of the exact head is due now, so the caller
        (which owns the GitHub client) requests it outside this transaction.
        The counter is consecutive: any observation that is not
        infrastructure-only resets it, so a head that recovers costs nothing
        later.  The re-run request is rate-limited by a bounded exponential
        backoff so an outage lasting hours cannot turn into a request per tick.
        """
        from sqlalchemy import update

        from src.database.tables import integration_source_ci

        repair = policy.root.repair
        now = time.time()
        infra_only = observation.state == "cancelled"
        if not infra_only:
            if (record["infra_observations"] or record["infra_reruns"]
                    or record["infra_rerun_at"] is not None):
                await conn.execute(update(integration_source_ci).where(*conditions).values(
                    infra_observations=0, infra_rerun_at=None, infra_reruns=0))
            return {"rerun": False, "infra_observations": 0}
        observations = int(record["infra_observations"]) + 1
        if observations >= repair.source_ci_infra_attempts:
            # Bounded: stop asking and name the blocker instead of looping.
            await conn.execute(update(integration_source_ci).where(*conditions).values(
                infra_observations=observations, infra_rerun_at=None))
            return {
                "rerun": False, "infra_observations": observations,
                "blocker": "source_ci_infrastructure", "outcome": "infra_blocked",
            }
        reruns = int(record["infra_reruns"])
        due_at = record["infra_rerun_at"]
        rerun = due_at is None or now >= due_at
        if rerun:
            delay = min(
                repair.source_ci_infra_backoff_seconds * (2 ** reruns),
                repair.source_ci_infra_backoff_max_seconds,
            )
            await conn.execute(update(integration_source_ci).where(*conditions).values(
                infra_observations=observations, infra_rerun_at=now + delay,
                infra_reruns=reruns + 1))
        else:
            await conn.execute(update(integration_source_ci).where(*conditions).values(
                infra_observations=observations))
        return {"rerun": rerun, "infra_observations": observations}

    async def _record_source_delivery(
        self, observation, source, proof, producer, conditions
    ) -> dict | None:
        """Record what was observed for audit, or refuse a proof that moved.

        The answer was gathered outside the hierarchy lock, so the exact source
        identity is re-read here under it: a generation that moved while git was
        read has no answer, and a refusal is returned rather than a proof --
        withholding a repair a later generation may still need would strand
        real work, which is the one failure this whole check must not have.

        Nothing reads this record back to decide eligibility
        (:func:`~src.integration.source_delivery.record_delivery_evidence`);
        it exists so an operator and ``aq task explain`` can see what was
        observed.  Returns ``None`` once it is durable, so the caller decides
        what it means; the returned dict is a refusal to surface instead.
        """
        from sqlalchemy import select, update

        from src.database.tables import integration_source_ci
        from src.integration.source_repairs import record_delivery_evidence

        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, source["project_id"])
            if await producer._pull_request_source_on(conn, observation.task_id) != source:
                return _failure("stale", "source changed before its delivery proof")
            current = (await conn.execute(select(integration_source_ci.c.evidence)
                       .where(*conditions))).mappings().one_or_none()
            if current is None:
                return _failure("stale", "source observation disappeared before its delivery proof")
            await conn.execute(update(integration_source_ci).where(*conditions).values(
                evidence=record_delivery_evidence(current["evidence"], proof)))
        return None

    async def repair_integration_source_ancestry(self, observation) -> dict:
        """Withdraw one exact source whose recorded base is not its ancestor.

        Records the exact rejection, then routes the repair: under the operator's
        ``root.repair.source_ci`` authorization the source is reopened on its own
        branch with feedback; otherwise the project supervisor is told once.  It
        takes a server-observed proof, so it is deliberately not a ``_cmd_``
        command that an agent, the CLI or MCP could reach.
        """
        from sqlalchemy import text

        from src.integration.source_ancestry import SourceAncestryObservation

        if not isinstance(observation, SourceAncestryObservation):
            return _failure("invalid", "source ancestry must be server-observed")
        identity = "source-ancestry:" + ":".join(str(part) for part in observation.identity())
        async with self.db._engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                               {"key": identity})
            return await self._repair_integration_source_ancestry(observation)

    async def _repair_integration_source_ancestry(self, observation) -> dict:
        from sqlalchemy import select
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from src.database.tables import messages, projects, task_context
        from src.integration.models import HierarchicalIntegrationPolicy
        from src.integration.review_evidence import ReviewEvidenceProducer
        from src.integration.source_ancestry import describe, repair_feedback

        producer = ReviewEvidenceProducer(self.db, None)
        source = observation.source
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, source["project_id"])
            current = await producer._pull_request_source_on(conn, observation.task_id)
            if not observation.matches(current):
                return {"success": True, "outcome": "stale",
                        "task_id": observation.task_id}
            evidence = await producer.record_ancestry_rejection_on(conn, observation)
            project = (await conn.execute(select(projects).where(
                projects.c.id == source["project_id"]))).mappings().one()
            policy_data = project["hierarchical_integration_policy"]
            policy = (HierarchicalIntegrationPolicy.model_validate(policy_data)
                      if policy_data else None)
            # One automatic reopen per exact identity: the same head coming back
            # means the repair did not happen, and reopening again would loop.
            reopened_before = (await conn.execute(
                select(task_context.c.id).where(
                    task_context.c.task_id == observation.task_id,
                    task_context.c.type == "reopen_feedback",
                    task_context.c.content.contains(evidence["id"]),
                ).limit(1)
            )).scalar_one_or_none() is not None
            if policy is None or not policy.root.repair.source_ci:
                why = "Automatic source repair is not authorized for this project."
            elif observation.reason != "source_base_not_ancestor":
                why = ("The recorded review tree does not match the head; that is a record "
                       "integrity fault no worker merge can repair.")
            elif reopened_before:
                why = ("The source was already reopened once for this exact identity and "
                       "closed again without a new head.")
            else:
                why = None
            if why is not None:
                await conn.execute(pg_insert(messages).values(
                    id=f"msg-source-ancestry-{evidence['id']}",
                    project_id=source["project_id"],
                    from_kind="system",
                    from_id="integration-admission",
                    to_kind="session",
                    to_id=f"supervisor-{source['project_id']}",
                    subject=f"Train source {observation.task_id} cannot be integrated",
                    body=(
                        f"{describe(observation)}. The train withdrew this exact identity "
                        f"(review evidence {evidence['id']}); valid sources keep moving. "
                        f"{why} Route the repair yourself, for example:\n"
                        f"aq task reopen-with-feedback --task-id {observation.task_id} "
                        "--feedback \"Merge the recorded base into the branch, preserving "
                        "history, then publish and close.\"\n\n"
                        + repair_feedback(observation)
                    ),
                    created_at=time.time(),
                    priority=50,
                    archive_after_inject=1,
                    body_kind="integration_source_ancestry",
                ).on_conflict_do_nothing(index_elements=[messages.c.id]))
                return {"success": True, "outcome": "supervisor_notified",
                        "task_id": observation.task_id, "evidence_id": evidence["id"]}
        # Reopen through the ordinary command: same task, branch and recorded
        # origin, so the repaired head is a new identity of the same source.
        # Re-read first: the reopen is its own transaction.
        async with self.db._engine.connect() as conn:
            if not observation.matches(
                await producer._pull_request_source_on(conn, observation.task_id)
            ):
                return {"success": True, "outcome": "stale",
                        "task_id": observation.task_id}
        reopened = await self._cmd_reopen_with_feedback({
            "task_id": observation.task_id, "feedback": repair_feedback(observation),
        })
        if reopened.get("error"):
            return _failure("runtime_error", reopened["error"])
        await self.db.log_event(
            "integration.source_ancestry_withdrawn",
            project_id=source["project_id"],
            task_id=observation.task_id,
            payload=json.dumps({
                "evidence_id": evidence["id"], "reason": observation.reason,
                "source_base": source["base"], "source_head": source["head"],
                "generation": int(source["generation"]), "merge_base": observation.merge_base,
                "detected_by": observation.detected_by, "batch_id": observation.batch_id,
            }),
        )
        return {"success": True, "outcome": "reopened", "task_id": observation.task_id,
                "evidence_id": evidence["id"]}

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

    @parent_engine_guard("command_target", outcome="human_required")
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
        elif principal.kind is PrincipalKind.PLAYBOOK:
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
            if allow_session_read and principal.project_id == project_id:
                return True
            # Supervisor recovery: a live, elevated, named supervisor may
            # re-drive the root train's own guarded subject commands (the
            # same ones its playbook calls).  Each re-derives authority, CI
            # evidence and fences server-side; a worker session never passes.
            if capability not in _SUPERVISOR_REDRIVE_CAPABILITIES or not principal.elevated:
                return False
            _operator, refusal = await integration_operator(getattr(self, "db", None), project_id)
            return refusal is None
        if principal.kind is PrincipalKind.PLAYBOOK:
            return bool(
                not principal.unresolved
                and principal.project_id == project_id
                and principal.policy.allows("aq_commands", capability)
            )
        return principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}

    def _integration_repository_git(self):
        """Read-only canonical Git port; no legacy publisher is constructed."""
        service = getattr(self.orchestrator, "repository_git", None)
        if service is not None:
            return service
        from src.integration.repository_git import RepositoryGit

        return RepositoryGit(
            self.db, data_dir=self.config.data_dir, git_manager=self.orchestrator.git,
        )

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
        from src.integration.status import IntegrationStatusService

        service = IntegrationStatusService(
            self.db,
            git_first=getattr(self.config.integration, "git_first", "shadow"),
            train=getattr(self.orchestrator, "integration_train", None),
            flow_problems=getattr(self.orchestrator, "promotion_flow_problems", None),
        )
        status = await (service.control_status(project_id) if args.get("control_only")
                        else service.status(project_id))
        if status is not None:
            from src.git.github_cli import GhRunner

            runner = getattr(self.orchestrator, "github_runner", None)
            if isinstance(runner, GhRunner):
                status["github"] = runner.activity()
        return {"outcome": "not_found", "project_id": project_id} if status is None else {
            "outcome": "status", **status}


    async def _integration_app_inputs(
        self, args: dict
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """``(refusal, inputs)`` shared by the read-only App-mode commands.

        The policy is the caller's ``policy`` when given, so the anchors can be
        prepared before the policy is bound, else the project's bound policy.
        The repository is ``repository_id`` when given, else the designated
        one.  Every identity comes from the daemon: the binding from the
        authenticated resolver and the credential from the GitHub client.
        """
        from pydantic import ValidationError

        from src.git.github_contracts import credential_identity_from_client
        from src.integration.models import HierarchicalIntegrationPolicy

        project_id = str(args.get("project_id") or "")
        if not project_id:
            return _failure("not_found", "project_id is required"), {}
        _label, refusal = await integration_operator(getattr(self, "db", None), project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal), {}
        project = await self.db.get_project(project_id)
        if project is None:
            return _failure("not_found", f"project {project_id} does not exist"), {}

        supplied = args.get("policy")
        raw_policy = supplied if supplied is not None else project.hierarchical_integration_policy
        if raw_policy is None:
            return _failure(
                "policy_missing", f"{project_id} has no bound integration policy; pass --policy FILE"
            ), {}
        try:
            policy = HierarchicalIntegrationPolicy.model_validate(raw_policy)
        except (ValidationError, TypeError, ValueError) as exc:
            return _failure("policy_invalid", f"the integration policy is invalid: {exc}"), {}

        repository_id = str(args.get("repository_id") or project.integration_repository_id or "")
        if not repository_id:
            return _failure(
                "repository_not_designated",
                f"{project_id} has no designated integration repository; pass --repository-id",
            ), {}
        repository = await self.db.get_repo(repository_id)
        if repository is None or repository.project_id != project_id:
            return _failure(
                "repository_mismatch", f"repository {repository_id} does not belong to {project_id}"
            ), {}
        if not repository.default_branch:
            return _failure(
                "repository_default_branch_missing",
                f"repository {repository_id} has no default branch",
            ), {}

        resolver = getattr(self.orchestrator, "github_repository_binding_resolver", None)
        factory = getattr(self.orchestrator, "github_client_factory", None)
        if resolver is None or factory is None:
            return _failure("provider_not_wired", "the daemon's GitHub client is not wired"), {}
        try:
            binding = resolver(repository)
            if inspect.isawaitable(binding):
                binding = await binding
        except Exception:  # noqa: BLE001 - any binding failure is the named refusal
            binding = None
        if binding is None:
            return _failure(
                "repository_binding_failed",
                f"repository {repository_id} has no authenticated GitHub binding "
                "(its origin must be an exact https://github.com/OWNER/REPO.git URL)",
            ), {}
        try:
            client = factory(binding)
            if inspect.isawaitable(client):
                client = await client
            identity = credential_identity_from_client(client)
        except Exception:  # noqa: BLE001 - any client failure is the named refusal
            client = identity = None
        if identity is None or getattr(client, "repository", None) != binding:
            return _failure("provider_binding_failed", "the GitHub client could not bind"), {}
        return None, {
            "project": project,
            "project_id": project_id,
            "policy": policy,
            "policy_source": "argument" if supplied is not None else "bound",
            "repository_id": repository_id,
            "repository": repository,
            "binding": binding,
            "client": client,
            "identity": identity,
        }

    async def _cmd_integration_trust_manifest(self, args: dict) -> dict:
        """Render the App-mode trust manifest and compare the default-branch copy.

        Read-only (App-mode integration train spec §6.1), on the inputs
        :meth:`_integration_app_inputs` resolves: ``repository_id`` and
        ``full_name`` from the authenticated binding, ``attestation_app_id``
        from the App client, and the committed copy read through the App at
        the default branch's exact SHA.
        """
        from src.git.github_contracts import GitHubAccessError, GitHubCredentialMode
        from src.integration import trust_manifest
        from src.integration.preflight import read_committed_trust_manifest
        from src.integration.promotion_steps import read_stored_promotion_flow

        refusal, inputs = await self._integration_app_inputs(args)
        if refusal is not None:
            return refusal
        binding, identity = inputs["binding"], inputs["identity"]
        repository = inputs["repository"]
        if identity.mode is not GitHubCredentialMode.APP:
            return _failure(
                "not_app_mode",
                "the trust manifest is an App credential mode anchor; this daemon uses "
                "existing-login credentials (integration.github_app is not configured)",
            )

        try:
            # Project DTOs omit this column; the retained reader is also used
            # by app-verify and functional preflight. Lightweight adapters may
            # expose it directly when they have no database engine.
            flow = await read_stored_promotion_flow(self.db, inputs["project_id"])
            if flow is None:
                flow = getattr(inputs["project"], "promotion_flow", None)
            manifest = trust_manifest.manifest_for_policy(
                inputs["policy"],
                canonical_repository_id=inputs["repository_id"],
                repository_id=binding.repository_id,
                full_name=binding.full_name,
                attestation_app_id=identity.app_id,
                promotion_flow=flow or (),
            )
        except trust_manifest.TrustManifestRefusal as exc:
            return _failure(exc.code, str(exc))
        text = trust_manifest.canonical_text(manifest)

        committed: dict[str, Any] = {
            "path": trust_manifest.TRUST_MANIFEST_PATH,
            "ref": repository.default_branch,
            "sha": None,
        }
        try:
            sha, raw = await read_committed_trust_manifest(
                inputs["client"], binding, repository.default_branch
            )
        except (GitHubAccessError, ValueError) as exc:
            committed.update(
                trust_manifest.compare(manifest, None).as_dict(),
                error=f"the default-branch copy could not be read: {exc}",
            )
        else:
            committed.update(sha=sha, **trust_manifest.compare(manifest, raw).as_dict())
        return {
            "success": True,
            "outcome": "manifest",
            "project_id": inputs["project_id"],
            "repository_id": inputs["repository_id"],
            "policy_source": inputs["policy_source"],
            "github_repository_id": binding.repository_id,
            "full_name": binding.full_name,
            "attestation_app_id": identity.app_id,
            "path": trust_manifest.TRUST_MANIFEST_PATH,
            "manifest": manifest,
            "text": text,
            "sha256": trust_manifest.text_sha256(text),
            "committed": committed,
        }

    async def _cmd_integration_app_verify(self, args: dict) -> dict:
        """Check everything App-mode runtime depends on, one item per concern.

        Read-only (App-mode integration train spec §6.2).  The items are
        :func:`src.integration.app_mode.evaluate`'s, the same ones the
        functional preflight turns into blockers and warnings, so the two
        cannot disagree.  Existing-login credentials are reported as the
        ``credential`` item's ``not_app_mode``, not refused.
        """
        from src.integration import app_mode

        refusal, inputs = await self._integration_app_inputs(args)
        if refusal is not None:
            return refusal
        from src.integration.promotion_steps import read_stored_promotion_flow

        binding = inputs["binding"]
        report = await app_mode.evaluate(
            app_mode.AppModeContext(
                project_id=inputs["project_id"],
                repository_id=inputs["repository_id"],
                default_branch=inputs["repository"].default_branch,
                binding=binding,
                client=inputs["client"],
                identity=inputs["identity"],
                policy=inputs["policy"],
                mode=getattr(inputs["project"], "hierarchical_integration_mode", None),
                policy_path=args.get("policy_path") if args.get("policy") is not None else None,
                repository_arg=args.get("repository_id") or None,
                promotion_flow=await read_stored_promotion_flow(self.db, inputs["project_id"]),
            )
        )
        return {
            "success": True,
            "outcome": "verified",
            "project_id": inputs["project_id"],
            "repository_id": inputs["repository_id"],
            "policy_source": inputs["policy_source"],
            "github_repository_id": binding.repository_id,
            "full_name": binding.full_name,
            "attestation_app_id": inputs["identity"].app_id,
            "default_branch": inputs["repository"].default_branch,
            **report.as_dict(),
        }


    async def _cmd_integration_eject(self, args: dict) -> dict:
        from src.commands.contracts.integration import IntegrationEjectArgs
        from src.integration.batches import BatchStore
        from src.integration.engine import current_policy_ejection
        from src.integration.train_controls import TrainControlError, TrainControls

        batch_id = str(args.get("batch_id") or "")
        task_id = str(args.get("task_id") or "")
        reason = str(args.get("reason") or "")
        if not batch_id.strip():
            return _failure("invalid_state", "batch_id is required")
        policy_ejection = current_policy_ejection(self.db, batch_id, task_id, reason)
        if policy_ejection is None:
            principal = current_principal() or TRUSTED_LOCAL
            _operator, refusal = await integration_operator(self.db, principal.project_id)
            if refusal:
                return _failure("unauthorized", refusal)

        batch = await BatchStore(self.db).get(batch_id)
        if batch is not None:
            operator, refusal = await integration_operator(self.db, batch.project_id)
            if refusal:
                return _failure("unauthorized", refusal)
            request = IntegrationEjectArgs.model_validate(args)
            if request.task_id not in {m.task_id for m in await BatchStore(self.db).members(batch.id)}:
                return _failure("not_a_member", "task is not a frozen batch member")
            train = getattr(self.orchestrator, "integration_train", None)
            if train is None:
                return _failure("refused", "the integration train is not active")
            try:
                targets = await train.targets.targets(time.time())
                target = next((target for target in targets if target.key == (
                    batch.project_id, batch.repository_id, batch.target_ref)), None)
                if target is None:
                    return _failure("refused", "batch has no active train target")
                lane = await train.lane_for(target)
                result = await TrainControls(self.db).eject(batch.id, request.task_id,
                    service=lane.service, dry_run=request.dry_run,
                    operator_id=operator, reason=request.reason)
            except TrainControlError as exc:
                return _failure(exc.outcome, str(exc))
            except (ValueError, GitError, OSError) as exc:
                return _failure("refused", str(exc))
            return {"success": True, **result}

        if policy_ejection is None:
            # An absent Git-first batch has no project binding. A project's
            # supervisor must get the same refusal for foreign and missing IDs.
            _operator, refusal = await integration_operator(self.db, None)
            if refusal:
                return _failure("unauthorized", refusal)
        if not await self.db.get_integration_batch(batch_id):
            return _failure("unknown_batch", "batch is missing")
        if not batch_id or not task_id or not reason.strip():
            return _failure("invalid_state", "batch_id, task_id and reason are required")
        if args.get("dry_run"):
            return _failure("invalid_state", "legacy batch ejection does not support preview")
        if policy_ejection is None:
            return _failure("unauthorized", "ejection requires the active root policy visit")
        operator_id = "service:root-reconciler"

        async def observe_resolution(resolution):
            batch = await self.db.get_integration_batch(resolution["batch_id"])
            if batch is None:
                return None
            service = await self._integration_candidate_service(batch)
            return await service.app_client.exact_head_ref(
                resolution["target_branch"].removeprefix("refs/heads/"),
            )

        from src.integration.gates import RootMemberEjection

        return await RootMemberEjection(self.db).eject(
            batch_id,
            task_id=task_id,
            reason=reason,
            operator_id=operator_id,
            resolution_observer=observe_resolution,
            policy_ejection=policy_ejection,
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






    async def _cmd_integration_reevaluate_repair(self, args: dict) -> dict:
        from src.commands.contracts.integration import IntegrationReevaluateRepairArgs

        request = IntegrationReevaluateRepairArgs.model_validate(args)
        _principal, refusal = await self._integration_operator_for_operation(request.operation_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        operation = await self.db.get_integration_operation(request.operation_id)
        if operation is None:
            return _failure("not_found", "operation does not exist")
        subject = None
        if request.expected_head_sha:
            subject = ({"kind": "parent", "generation": request.expected_generation,
                        "head_sha": request.expected_head_sha}
                       if operation["target_kind"] == "parent" else
                       {"kind": "batch", "revision": request.expected_generation,
                        "candidate_sha": request.expected_head_sha})
        service = self._integration_repair_service()
        result = await service.reevaluate(
            request.operation_id, dry_run=request.dry_run, expected_subject=subject,
            expected_episode_id=request.expected_episode_id,
            expected_generation=request.expected_generation, expected_stage=request.expected_stage,
            expected_fence_token=request.expected_fence_token, reason=request.reason,
            expected_snapshot_digest=request.expected_snapshot_digest,
        )
        return _with_reason(result["outcome"] in {
            "would_reevaluate", "reevaluated", "already_settled",
        }, result)





    async def _cmd_integration_recover_candidate_member(self, args: dict) -> dict:
        """Recover a durable pushed root-candidate repair."""
        from pydantic import ValidationError
        from sqlalchemy import select

        from src.commands.contracts.integration import IntegrationRecoverCandidateMemberArgs
        from src.database.tables import integration_candidate_resolutions
        from src.integration.promotion_contracts import CandidateAuthorizationError

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
        if task.created_by_kind == "integration_repair":
            result = await self._integration_repair_service().reserve_delegate(task.id)
        else:
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
            "success": result["outcome"] in {
                "would_open", "opened", "would_collect", "collecting", "nothing_to_redrive",
            },
            "dry_run": request.dry_run,
            **result,
        }


    async def _cmd_integration_authorize_root(self, args: dict) -> dict:
        """Record an operator's authorization of one exact completed train root source."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationAuthorizeRootArgs
        from src.integration.root_authorization import RootAuthorization

        try:
            request = IntegrationAuthorizeRootArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid root authorization request: {exc}")
        task = await self.db.get_task(request.task_id)
        principal, refusal = await integration_operator(
            self.db, task.project_id if task is not None else None
        )
        if refusal is not None:
            return _failure("unauthorized", refusal)
        result = await RootAuthorization(self.db).run(
            request.task_id, dry_run=request.dry_run,
            expected_head_sha=request.expected_head_sha, reason=request.reason,
            operator_id=principal,
        )
        return {
            "success": result["outcome"] in {
                "would_authorize", "authorized", "already_authorized",
            },
            "dry_run": request.dry_run, **result,
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

    async def _cmd_integration_reopen_collection(self, args: dict) -> dict:
        """Recover a suspended producer, cancelled collection or failed verifier."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationReopenCollectionArgs
        from src.integration.cancelled_collection_recovery import CancelledCollectionRecovery
        from src.integration.failed_verification_recovery import FailedVerificationRecovery
        from src.integration.suspended_parent_recovery import SuspendedParentRecovery

        try:
            request = IntegrationReopenCollectionArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid collection reopen request: {exc}")
        task = await self.db.get_task(request.task_id)
        from src.integration.owner_guards import active_parent_scope
        if task is not None and active_parent_scope(self.db, task.id):
            principal, refusal = "policy:parent-reconciler", None
        else:
            principal, refusal = await integration_operator(
                self.db, task.project_id if task is not None else None
            )
        if refusal is not None:
            return _failure("unauthorized", refusal)
        repair = self._integration_repair_service()

        async def dispatch(operation_id: str, stage: int) -> dict:
            return await repair.dispatch(operation_id, stage)

        checkpoint = await self.db.get_integration_checkpoint(request.task_id)
        if checkpoint is not None and checkpoint["state"] == "awaiting_children":
            owner = await BranchOwnership(self.db).get_owner(
                BranchKey(repository_id=checkpoint["repository_id"], branch=checkpoint["branch"])
            )
            operation = await self.db.get_active_parent_integration_operation(request.task_id)
            if (operation is not None and operation["state"] in {"active", "escalated"}
                    and owner is not None and owner["owner_role"] == "worker"):
                result = await SuspendedParentRecovery(
                    self.db, self._hierarchy_integration_service()
                ).run(
                    request.task_id, dry_run=request.dry_run,
                    expected_head_sha=request.expected_head_sha, reason=request.reason,
                    operator_id=principal,
                )
                return {
                    "success": result["outcome"] in {"would_reopen", "reopened", "nothing_to_reopen"},
                    "dry_run": request.dry_run, **result,
                }
        recovery_type = (
            FailedVerificationRecovery
            if checkpoint is not None and checkpoint["state"] in {"verifying", "integration_ready"}
            else CancelledCollectionRecovery
        )
        recovery_options = {"dispatch": dispatch}
        if recovery_type is FailedVerificationRecovery:
            recovery_options["confirm_handoff"] = getattr(
                getattr(self, "orchestrator", None), "aconfirm_integration_owner_handoff", None
            )
        result = await recovery_type(
            self.db, self._integration_promotion_service(), **recovery_options
        ).run(
            request.task_id,
            dry_run=request.dry_run,
            expected_head_sha=request.expected_head_sha,
            reason=request.reason,
            operator_id=principal,
        )
        return {
            "success": result["outcome"] in {"would_reopen", "reopened", "nothing_to_reopen"},
            "dry_run": request.dry_run,
            **result,
        }


    async def _cmd_integration_rebind_repair(self, args: dict) -> dict:
        """Prove a live repair candidate and reserve it under the current intent."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRebindRepairArgs
        from src.integration.promotion_contracts import PromotionError
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

    async def _cmd_integration_recover_parent_head(self, args: dict) -> dict:
        """Reconcile a completed post-collection repair with its immutable receipts."""
        from src.commands.contracts.integration import IntegrationRecoverParentHeadArgs
        from src.integration.parent_repair_heads import ParentHeadRecovery
        from src.integration.promotion_contracts import PromotionError

        try:
            request = IntegrationRecoverParentHeadArgs.model_validate(args)
        except ValueError as exc:
            return _failure("blocked", str(exc))
        principal, refusal = await self._integration_operator_for_operation(request.operation_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            result = await ParentHeadRecovery(self._integration_promotion_service()).run(
                request, principal=principal,
            )
        except (PromotionError, GitError, ValueError, HierarchyError) as exc:
            return _failure("blocked", str(exc))
        return {
            "success": result["outcome"] in {"would_recover", "recovered", "already_recovered"},
            **result,
        }

    async def _cmd_integration_recover_preserved_repair(self, args: dict) -> dict:
        """Consume an audited completed candidate without renewing repair authority."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRecoverPreservedRepairArgs
        from src.integration.preserved_repair import PreservedRepairRecovery
        from src.integration.promotion_contracts import PromotionError

        try:
            request = IntegrationRecoverPreservedRepairArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("blocked", str(exc))
        principal, refusal = await self._integration_operator_for_operation(request.operation_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            result = await PreservedRepairRecovery(
                self._integration_promotion_service(), self._integration_repair_service()
            ).run(request, principal=principal)
        except (PromotionError, GitError, ValueError, BranchBusy, StaleFence) as exc:
            return _failure("blocked", str(exc))
        return {
            "success": result["outcome"] in {"would_recover", "recovered", "already_recovered"},
            **result,
        }

    async def _cmd_integration_rebind_detached_repair(self, args: dict) -> dict:
        """Rebind a detached debug stage frozen on an unpublished head to its conflict."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationRebindDetachedRepairArgs
        from src.integration.detached_repair_rebind import DetachedRepairRebind
        from src.integration.promotion_contracts import PromotionError

        try:
            request = IntegrationRebindDetachedRepairArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("blocked", f"invalid detached repair rebind request: {exc}")
        principal, refusal = await self._integration_operator_for_operation(
            request.operation_id
        )
        if refusal is not None:
            return _failure("unauthorized", refusal)
        try:
            result = await DetachedRepairRebind(
                self._integration_promotion_service(), self._integration_repair_service()
            ).run(
                request.operation_id,
                dry_run=request.dry_run,
                expected_stage=request.expected_stage,
                expected_remote_head_sha=request.expected_remote_head_sha,
                reason=request.reason,
                principal=principal,
            )
        except (PromotionError, GitError, ValueError) as exc:
            return _failure("blocked", str(exc))
        return {
            "success": result["outcome"] in {"would_rebind", "rebound", "already_rebound"},
            **result,
        }



    async def _cmd_integration_close_delivered_pr(self, args: dict) -> dict:
        """Close one open PR only once Git proves its work is on the default branch."""
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationCloseDeliveredPrArgs
        from src.integration.pr_delivery import DeliveredPullRequestClosure
        from src.integration.pr_cleanup import SWEEP_PRINCIPAL

        try:
            request = IntegrationCloseDeliveredPrArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("invalid", f"invalid delivered-PR close request: {exc}")
        caller = current_principal()
        sweep = (caller is not None and caller.kind is PrincipalKind.SERVICE
                 and caller.service_name == SWEEP_PRINCIPAL)
        if sweep:
            principal, refusal = SWEEP_PRINCIPAL, None
        else:
            principal, refusal = await integration_operator(self.db, request.project_id)
        if refusal is not None:
            return _failure("unauthorized", refusal)
        result = await DeliveredPullRequestClosure(
            self.db, self._integration_repository_git()
        ).run(
            request.project_id,
            request.pr_number,
            dry_run=request.dry_run,
            expected_head_sha=request.expected_head_sha,
            reason=request.reason,
            operator_id=principal,
            aq_only=sweep,
        )
        return {
            "success": result["outcome"] in {"would_close", "closed", "nothing_to_close"},
            "dry_run": request.dry_run,
            **result,
        }

    def _integration_train_service(self):
        service = getattr(self.orchestrator, "integration_train_service", None)
        if service is not None:
            return service
        from src.integration.scheduler import TrainService
        from src.integration.migration_heads import MigrationInspector
        from src.integration.source_ci import RootAdmissionReader

        return TrainService(
            self.db,
            default_mode=self.config.integration.default_mode,
            migration_inspector=MigrationInspector(self._integration_promotion_service()),
            admission_reader=RootAdmissionReader(self._integration_promotion_service()),
            delivery_observer=getattr(self.orchestrator, "delivery_observer", None),
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
        confirm_published_repair = None
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
            if repair_session.lifecycle == "pool" and app_client is not None:
                callback = getattr(
                    self.orchestrator, "aconfirm_integration_pool_published_repair_handoff", None
                )
                if callback is not None:
                    async def confirm_published_repair(owner, reservation_id):
                        return await callback(
                            owner, reservation_id, remote_head_reader=app_client.exact_head_ref
                        )
        return CandidateService(
            self.db,
            data_dir=self.config.data_dir,
            git_manager=self.orchestrator.git,
            app_client=app_client,
            forge_provider=app_client,
            repair_service=self._integration_repair_service(),
            branch_ownership=branch_ownership,
            confirm_published_repair=confirm_published_repair,
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
        from src.integration.promotion_contracts import RootPromotionInvariantError

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
        return _with_reason(
            result.outcome in {"promoted", "already_promoted"}, result.model_dump(mode="json")
        )

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
            # A materialized count that disagrees with the persisted rows is
            # only actionable while work remains. Every persisted item already
            # complete leaves nothing to clean, so the batch is complete
            # whatever the count says: reporting invariant_error there held the
            # project lease and blocked the next seal for delivered work.
            if total > 0 and not conflicts and completed == total:
                outcome = "complete" if advanced else "already_complete"
            else:
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
        if request.expected_base_sha is not None and not moved_main:
            # A built revision whose construction base is not the caller's
            # observed main is stale: ``build`` would answer ``already_built``
            # for it forever (the reconciler's base-moved loop).
            from sqlalchemy import select

            from src.database.tables import integration_candidate_revisions

            async with self.db._engine.connect() as conn:
                built_base = (
                    await conn.execute(
                        select(integration_candidate_revisions.c.construction_base_sha).where(
                            integration_candidate_revisions.c.batch_id == request.batch_id,
                            integration_candidate_revisions.c.revision
                            == batch["current_revision"],
                            integration_candidate_revisions.c.head_sha.is_not(None),
                        )
                    )
                ).scalar_one_or_none()
            moved_main = built_base is not None and built_base != request.expected_base_sha
        if moved_main and batch["policy_snapshot"].get("on_main_moved", "rebuild") != "rebuild":
            return {
                "success": False,
                "outcome": "base_moved",
                "batch_id": request.batch_id,
                "revision": int(batch["current_revision"]),
            }
        rebuilt = False
        if moved_main and getattr(service, "app_client", None) is not None:
            rebuilt = True
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
                rebuilt = True
                result = await service.rebuild(
                    request.batch_id, int(batch["current_revision"]), new_base
                )
        if (
            request.expected_revision is not None
            and result.revision != request.expected_revision
            and not (rebuilt and result.outcome in {"built", "already_built"})
        ):
            return _failure("stale_revision", "candidate revision changed during build")
        return _with_reason(
            result.outcome in {"empty", "built", "already_built"}, result.model_dump(mode="json")
        )

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
        from src.integration.scheduler import empty_seal_request

        try:
            request = IntegrationReleaseArgs.model_validate(args)
        except ValidationError as exc:
            return _failure("runtime_error", f"invalid integration release request: {exc}")
        batch = await self.db.get_integration_batch(request.batch_id)
        # An empty seal keeps no row; its id names the project it swept.
        empty_seal = empty_seal_request(request.batch_id) if batch is None else None
        if batch is None and empty_seal is None:
            return _failure("stale", "integration batch does not exist")
        project_id = batch["project_id"] if batch is not None else empty_seal[0]
        if not await self._integration_delivery_authorized(project_id, "integration_release"):
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
            subject_policy_loader=getattr(self.orchestrator, "_load_playbook_artifact", None)
            if getattr(self.orchestrator, "parent_subject_runtime", None) is not None else None,
        )

    def _integration_repair_service(self):
        service = getattr(self.orchestrator, "repair_service", None)
        if service is not None:
            return service
        from src.integration.repair import RepairService
        from src.integration.owner_recovery import owner_recovery_for

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
            owner_recovery=owner_recovery_for(self.orchestrator),
            promotion=self._integration_promotion_service(),
        )

    async def _integration_operation_project_id(self, operation: dict) -> str | None:
        from src.integration.operation_ownership import operation_project_id

        return await operation_project_id(self.db, operation)

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
            "success": result["outcome"] in {"continue", "started", "escalate"},
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
        from src.integration.promotion_contracts import PromotionError, PromotionSourceMoved

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
        if result["outcome"] in {"completed", "already_completed"}:
            # The completion is already committed. Evidence is best effort;
            # the review poller retries under the same authorization and Git
            # guards after a crash, unavailable remote, or delayed PR opening.
            import logging
            from src.integration.review_evidence import ReviewEvidenceProducer

            try:
                project = await self.db.get_project(task.project_id)
                if project.hierarchical_integration_mode == "train":
                    await ReviewEvidenceProducer(
                        self.db, self._integration_repository_git(),
                    ).snapshot_authorized(
                        request.task_id, reviewed_sha=request.head_sha,
                        policy_generation=project.hierarchical_integration_generation,
                    )
            except Exception:
                logging.getLogger(__name__).warning(
                    "Parent %s completed; exact review evidence awaits poller retry",
                    request.task_id, exc_info=True,
                )
        return {
            "success": result["outcome"] in {"completed", "already_completed"},
            **result,
        }

    async def _cmd_delivery_promote(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import DeliveryPromoteArgs
        from src.integration.models import PromotionInput
        from src.integration.ownership import BranchBusy, StaleFence
        from src.integration.promotion_contracts import (
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
        except (PromotionInvariantError, ValueError) as exc:
            return _failure("runtime_error", str(exc))
        except (PromotionRuntimeError, GitError) as exc:
            return _failure("runtime_error", str(exc))
        return self._promotion_result("promoted", promoted)

    async def _cmd_integration_reconcile_promotion(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationReconcilePromotionArgs
        from src.integration.ownership import BranchBusy, StaleFence
        from src.integration.promotion_contracts import (
            PromotionConflict,
            PromotionInvariantError,
            PromotionNotApplied,
            PromotionRecovery,
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
            service = self._integration_promotion_service()
            if parsed.fence is None:
                value = await service.reconcile(parsed.intent_id)
            else:
                value = await service.reconcile(parsed.intent_id, fence=parsed.fence)
        except PromotionRecovery as exc:
            return self._promotion_result(
                exc.outcome, exc.value, success=exc.outcome in {"continued", "superseded"}
            )
        except (PromotionTargetMoved, StaleFence, BranchBusy) as exc:
            return _failure("target_moved", str(exc))
        except PromotionNotApplied as exc:
            return _failure("not_applied", str(exc))
        except (PromotionConflict, PromotionInvariantError, ValueError) as exc:
            return _failure("invariant_error", str(exc))
        except (PromotionRuntimeError, GitError) as exc:
            return _failure("runtime_error", str(exc))
        return self._promotion_result("applied", value)

    async def _cmd_integration_resolve_conflict(self, args: dict) -> dict:
        from pydantic import ValidationError

        from src.commands.contracts.integration import IntegrationResolveConflictArgs
        from src.integration.models import ConflictResolutionInput
        from src.integration.ownership import BranchBusy, StaleFence
        from src.integration.promotion_contracts import (
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
        from src.integration.promotion_contracts import (
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
        from src.integration.promotion_contracts import (
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
                if (accepted.outcome in {"accepted", "already_accepted"}
                    and await reconciler_batch_subject(self.db, batch["id"], include_done=True) is None):
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
                "invariant": accepted.invariant,
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
            if (accepted.outcome in {"accepted", "already_accepted"}
                and await reconciler_batch_subject(self.db, batch["id"], include_done=True) is None):
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
            "invariant": accepted.invariant,
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
        from src.integration.promotion_contracts import (
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
        from src.integration.development import DevelopmentPrimitives
        from src.jobs.adapters import PublisherJobs
        service = getattr(self.orchestrator, "development_integration", None)
        if service is not None:
            service.job_client = PublisherJobs(self)
            return service
        return DevelopmentPrimitives(
            self.db, data_dir=self.config.data_dir, git=self.orchestrator.git,
            job_client=PublisherJobs(self),
        )
