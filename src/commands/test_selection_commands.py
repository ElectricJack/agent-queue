"""Authorization and events for explicit, immutable test-selection requests."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.queries.test_selection_queries import PromotionActive
from src.test_selection.service import SelectionRequest, SelectionService
from src.test_selection.static_impact import PytestImpactedAdapter
from src.test_selection.typesafe import SdkTransport


def _error(code: str, message: str, **details) -> dict:
    return {"success": False, "error_code": code, "error": message, **details}


def _sdk_transport_factory(config):
    key = os.environ.get(config.api_key_env)
    return (
        SdkTransport(api_key=key, base_url=config.base_url) if config.jev_enabled and key else None
    )


class TestSelectionCommandsMixin:
    """Commands own authority; the selection service owns computation and storage."""

    def _test_selection_service(self) -> SelectionService:
        service = getattr(self.orchestrator, "test_selection_service", None)
        if service is None:

            async def default_branch(project_id):
                project = await self.db.get_project(project_id)
                return await self.orchestrator._get_default_branch(project)

            service = SelectionService(
                db=self.db,
                git=self.orchestrator.git,
                config_getter=lambda: self.config.test_selection,
                static=PytestImpactedAdapter(
                    timeout_seconds=self.config.test_selection.static_timeout_seconds
                ),
                transport_factory=_sdk_transport_factory,
                default_branch_getter=default_branch,
            )
            self.orchestrator.test_selection_service = service
        return service

    async def _selection_workspace_valid(self, workspace) -> bool:
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            return False
        if not await asyncio.to_thread(Path(workspace).is_dir):
            return False
        result = await self.orchestrator.git.arun_git_result(
            ["rev-parse", "--show-toplevel"], cwd=workspace
        )
        return result.returncode == 0

    async def _cmd_test_select(self, args: dict) -> dict:
        cfg = self.config.test_selection
        if not cfg.enabled:
            return _error("disabled", "test selection is disabled")
        mode = args.get("mode", "shadow")
        if mode not in {"plan_only", "shadow", "enforce"}:
            return _error("invalid_mode", "mode must be plan_only, shadow or enforce")
        if mode == "enforce" and not cfg.enforce_enabled:
            return _error("enforce_not_enabled", "test-selection enforcement is disabled")
        if mode == "enforce" and args.get("narrowing_flags"):
            return _error(
                "narrowing_flags",
                "enforce cannot narrow the selected union",
                narrowing_flags=args["narrowing_flags"],
            )
        principal = current_principal() or TRUSTED_LOCAL
        task_id = args.get("task_id")
        epoch = args.get("claim_epoch")
        session_id = None
        if principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}:
            if "workspace" in args:
                return _error("spoofed_workspace", "workspace is derived from the session")
            session_id = principal.session_id
            if not session_id:
                return _error("out_of_scope", "a session holding a task is required")
            session = await self.db.get_session(session_id)
            task_id = task_id or (session.task_id if session else None)
            if session is None or not task_id:
                return _error("out_of_scope", "the session holds no task")
            if (
                session.project_id != principal.project_id
                or args.get("project_id", session.project_id) != session.project_id
                or (principal.task_id is not None and principal.task_id != task_id)
                or session.state not in {"starting", "running"}
                or session.desired_state != "running"
            ):
                return _error("out_of_scope", "the task is outside the live session scope")
            err = await self._assert_session_owns(task_id, session_id=session_id, claim_epoch=epoch)
            if err:
                return {**err, "error_code": err.get("result", "out_of_scope")}
            task = await self.db.get_task(task_id)
            if task.project_id != session.project_id or (
                session.last_claim_epoch is not None
                and session.last_claim_epoch != task.claim_epoch
            ):
                return _error("stale_claim", "the session claim is no longer current")
            workspace, project_id = session.work_dir, session.project_id
            full_suite_authorized = False
        elif principal.kind is PrincipalKind.LOCAL:
            workspace = args.get("workspace")
            if not workspace:
                return _error("workspace_required", "local selection requires a workspace")
            project_id = args.get("project_id")
            if not project_id:
                return _error("out_of_scope", "local selection requires project_id")
            full_suite_authorized = True
        else:
            return _error("out_of_scope", "test selection requires a held task or local operator")
        if not await self._selection_workspace_valid(workspace):
            return _error("workspace_invalid", "workspace must be an absolute git directory")
        request = SelectionRequest(
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            claim_epoch=epoch,
            workspace=workspace,
            mode=mode,
            base_ref=args.get("base_ref"),
            targets=tuple(args.get("targets", ())),
            jev=bool(args.get("jev", True)),
            marker_policy=args.get("marker_policy", "default"),
            acceptance_commands=tuple(args.get("acceptance_commands", ())),
        )
        record = await self._test_selection_service().select(request)
        await self.orchestrator.bus.emit(
            "test_selection.recorded.v1",
            {
                "project_id": project_id,
                "selection_id": record["id"],
                "mode": mode,
                "task_id": task_id,
                "full_required": record["full_required"],
                "jev_status": record["jev_status"],
                "final_count": len(record["final_modules"]),
                "fallback_count": len(record["fallback_modules"]),
            },
        )
        fields = (
            "mode",
            "recorded",
            "full_required",
            "final_modules",
            "fallback_modules",
            "mandatory_modules",
            "static_modules",
            "jev_modules",
            "jev_status",
            "fallback_reason",
            "jev_used_for_omission",
            "reasons",
            "argv",
            "pending_obligations",
        )
        return {
            "success": True,
            "selection_id": record["id"],
            "record": record,
            "full_suite_authorized": full_suite_authorized,
            "ordered": [m for m in record["argv"][0] if m in record["final_modules"]],
            **{field: record[field] for field in fields},
        }

    async def _visible_test_selection(self, selection_id):
        record = await self.db.get_test_selection(selection_id)
        if record is None:
            return None, _error("not_found", "test selection was not found")
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}:
            session = (
                await self.db.get_session(principal.session_id) if principal.session_id else None
            )
            if (
                session is None
                or not session.task_id
                or record["task_id"] != session.task_id
                or record["project_id"] != principal.project_id
                or session.project_id != principal.project_id
                or (principal.task_id is not None and principal.task_id != session.task_id)
            ):
                return None, _error("out_of_scope", "selection does not belong to the held task")
        elif principal.kind is not PrincipalKind.LOCAL:
            return None, _error("out_of_scope", "selection requires a held task or local operator")
        return record, None

    async def _cmd_test_selection_recheck(self, args):
        _, err = await self._visible_test_selection(args["selection_id"])
        if err:
            return err
        return {
            "success": True,
            **await self._test_selection_service().recheck(args["selection_id"]),
        }

    async def _cmd_test_selection_observe(self, args):
        _, err = await self._visible_test_selection(args["selection_id"])
        if err:
            return err
        principal = current_principal() or TRUSTED_LOCAL
        try:
            row = await self._test_selection_service().observe(
                args["selection_id"],
                kind="execution",
                source="local" if principal.kind is PrincipalKind.LOCAL else "worker",
                exit_code=args["exit_code"],
                duration_ms=args["duration_ms"],
                executed_modules=args["executed_modules"],
                failed_node_ids=args.get("failed_node_ids", []),
                payload=args.get("payload", {}),
            )
        except LookupError:
            return _error("not_found", "test selection was removed")
        return {"success": True, "observation_id": row["id"]}

    async def _cmd_test_selection_show(self, args):
        row, err = await self._visible_test_selection(args["selection_id"])
        if err:
            return err
        return {
            "success": True,
            "selection": row,
            "observations": await self.db.list_test_selection_observations(args["selection_id"]),
        }

    def _test_selection_project_scope(self, project_id):
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL:
            return None
        if (
            principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}
            and principal.elevated
            and (principal.project_id is None or principal.project_id == project_id)
        ):
            return None
        return _error("out_of_scope", "selection policy and list require an operator")

    async def _cmd_test_selection_list(self, args):
        err = self._test_selection_project_scope(args["project_id"])
        if err:
            return err
        rows = await self.db.list_test_selections(
            project_id=args["project_id"],
            task_id=args.get("task_id"),
            limit=args.get("limit", 50),
            before=args.get("before"),
        )
        return {"success": True, "selections": rows}

    async def _cmd_test_selection_policy_show(self, args):
        err = self._test_selection_project_scope(args["project_id"])
        if err:
            return err
        cfg = self.config.test_selection
        latest = await self.db.list_test_selections(project_id=args["project_id"], limit=1)
        digests = (
            {k: latest[0][k] for k in ("catalogue_digest", "rules_digest", "policy_digest")}
            if latest
            else None
        )
        return {
            "success": True,
            "config": {
                k: getattr(cfg, k) for k in ("enabled", "jev_enabled", "enforce_enabled", "model")
            },
            "promotion": await self.db.active_test_selection_promotion(
                project_id=args["project_id"]
            ),
            "latest_digests": digests,
        }

    async def _cmd_test_selection_promote(self, args):
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            return _error("out_of_scope", "test-selection promotion requires local operator")
        try:
            row = await self.db.insert_test_selection_promotion(
                {
                    **{
                        k: args[k]
                        for k in (
                            "project_id",
                            "model",
                            "question_schema_version",
                            "catalogue_digest",
                            "rules_digest",
                            "policy_digest",
                            "evidence",
                        )
                    },
                    "promoted_by": "operator",
                    "promoted_at": time.time(),
                }
            )
        except PromotionActive:
            return _error("promotion_active", "the project already has an active promotion")
        await self.orchestrator.bus.emit(
            "test_selection.promoted.v1",
            {
                "project_id": row["project_id"],
                "promotion_id": row["id"],
                "model": row["model"],
            },
        )
        return {"success": True, "promotion": row}

    async def _cmd_test_selection_revoke(self, args):
        if (current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL:
            return _error("out_of_scope", "test-selection promotion requires local operator")
        revoked = await self.db.revoke_test_selection_promotion(
            args["promotion_id"], now=time.time(), reason=args["reason"]
        )
        if revoked:
            await self.orchestrator.bus.emit(
                "test_selection.revoked.v1",
                {
                    "promotion_id": args["promotion_id"],
                    "reason": args["reason"],
                },
            )
        return {"success": True, "revoked": revoked}
