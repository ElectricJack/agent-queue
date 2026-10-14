"""Verified wait command boundary; lifecycle owners supply async implementations.

``orchestrator.wait_v2_handlers`` maps command names to async callables accepting
``(args, *, principal, scope, handler)``. All names and arguments are contracted;
scope is derived here, never taken from an agent's request. Owners must repeat
the claim/generation fence inside their mutation transaction. Missing handlers
refuse before any mutation, and v2 writes require the explicit activation flag.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError
from sqlalchemy import select

from src.agent_waits import WaitError as LegacyWaitError
from src.commands.contracts.wait_v2 import (
    SERVICE_WAIT_COMMANDS,
    SUPERVISOR_WAIT_COMMANDS,
    WAIT_V2_ARGS,
    caller_allowed,
    operator_wait_grant,
)
from src.commands.principal import (
    ExecutionPrincipal,
    PrincipalKind,
    current_principal,
    principal_context,
)
from src.database.tables import wait_records
from src.waits.conditions import Scope, WaitError


def refusal(code, message, **details):
    return {"success": False, "error_code": code, "error": str(message), **details}


def service_source_commit(handler, service):
    """A source watcher's ``commit(event)``: ``wait_source_commit`` as a named service."""

    async def commit(event):
        with principal_context(ExecutionPrincipal.service(service)):
            return await handler.execute("wait_source_commit", event)

    return commit


class WaitV2CommandsMixin:
    def _wait_v2_enabled(self):
        return getattr(getattr(self.config, "wait_policy", None), "v2_enabled", False) is True

    async def _wait_v2_row(self, wait_id):
        async with self.db._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(wait_records).where(
                            (wait_records.c.id == wait_id) | (wait_records.c.legacy_id == wait_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def _wait_v2_scope(self, args, *, mutation=False):
        principal = current_principal()
        if principal.kind == PrincipalKind.SERVICE:
            if (
                not args.get("project_id")
                or args.get("owner_kind")
                not in {
                    "scheduler",
                    "integration",
                    "playbook",
                }
                or not args.get("owner_id")
            ):
                raise WaitError("wait.out_of_scope", "service registration needs an owner scope")
            return Scope(
                args["project_id"],
                args["owner_kind"],
                args["owner_id"],
                principal="service",
                claim_epoch=args.get("claim_epoch"),
            )
        if principal.kind == PrincipalKind.PLAYBOOK:
            if not principal.project_id or not principal.parent_run_id:
                raise WaitError("wait.out_of_scope", "playbook registration needs a bound run")
            if args.get("project_id", principal.project_id) != principal.project_id:
                raise WaitError("wait.out_of_scope", "playbook project mismatch")
            return Scope(
                principal.project_id, "playbook", principal.parent_run_id, principal="playbook"
            )
        try:
            identity = await self._wait_identity(args, mutation=mutation)
        except LegacyWaitError as exc:
            raise WaitError("wait.out_of_scope", str(exc)) from exc
        if identity is None:
            return None
        session = await self.db.get_session(identity["session_id"])
        owner_kind = "task" if session.task_id else "session"
        return Scope(
            identity["project_id"],
            owner_kind,
            session.task_id or session.id,
            session_id=session.id,
            instance_token=session.instance_token,
            claim_epoch=identity["claim_epoch"],
            elevated=principal.elevated,
        )

    async def _wait_v2_register(self, values, args):
        from src.waits.legacy import translate_registration
        from src.waits.registry import build_registry

        principal = current_principal()
        if principal is None or principal.kind not in {
            PrincipalKind.SESSION,
            PrincipalKind.PLAYBOOK,
            PrincipalKind.SERVICE,
        }:
            return refusal("wait.out_of_scope", "registration requires an authenticated owner")
        if not self._wait_v2_enabled():
            return refusal("wait.disabled", "verified wait registration is disabled")
        if principal.kind != PrincipalKind.SERVICE and (values.owner_kind or values.owner_id):
            return refusal("wait.out_of_scope", "owner scope is server-derived")
        if values.schema_version == 1:
            if getattr(self.config.wait_policy, "v1_bodies", "translate") == "refuse":
                return refusal(
                    "wait.upgrade_required",
                    "use a schema_version=2 condition",
                    example={
                        "schema_version": 2,
                        "condition": {"kind": "task", "task_id": "producer-task"},
                        "idempotency_key": "settlement",
                    },
                )
            condition = translate_registration(values)
        else:
            condition = dict(values.condition)
            if values.kind:
                condition.setdefault("kind", values.kind)
        # Shape validation is pure; authoritative verification and subscribe/reread
        # belong in the runtime callback's single registration transaction.
        normalised = build_registry().normalise(condition)
        scope = await self._wait_v2_scope(args, mutation=True)
        params = values.model_dump(exclude_none=True)
        params.update(schema_version=2, condition=normalised.as_dict())
        for key in ("kind", "ref", "after_seq", "due_at", "timeout", "owner_kind", "owner_id"):
            params.pop(key, None)
        result = await self._wait_v2_invoke("wait_register", params, scope=scope)
        if values.timeout is not None:
            result["warnings"] = [*result.get("warnings", []), "timeout_ignored"]
        return result

    async def _wait_v2_get(self, values, args):
        await self._wait_v2_authorize_target(args)
        if values.consume:
            acknowledgement = await self._wait_v2_command(
                "wait_ack",
                {
                    "wait_id": values.wait_id,
                    **{
                        k: v
                        for k, v in args.items()
                        if k in {"claim_epoch", "project_id", "task_id", "session_id"}
                    },
                },
            )
            if not acknowledgement["success"]:
                return acknowledgement
        record = await self._wait_v2_row(values.wait_id)
        if record is None:
            raise WaitError("wait.source_unresolved", "wait not found")
        receipt = (record.get("decision_inputs") or {}).get("registration_receipt", {})
        return {
            "success": True,
            "wait": record,
            "wait_id": record["id"],
            "condition": record["condition"],
            **{
                key: receipt.get(key)
                for key in ("evidence", "decision", "escalate_at", "ceiling_at", "continuation_id")
            },
            "next_step": "continue"
            if values.consume
            else "end_turn"
            if record["outcome"] == "pending"
            else "acknowledge",
        }

    async def _wait_v2_list(self, args):
        from src.commands.contracts.wait import WaitListArgs

        values = WaitListArgs.model_validate(args)
        filters = await self._wait_v2_read_filter(args)
        stmt = select(wait_records)
        for key, value in filters.items():
            if value is not None:
                stmt = stmt.where(wait_records.c[key] == value)
        stmt = (
            stmt.order_by(wait_records.c.created_at.desc(), wait_records.c.id)
            .limit(values.limit)
            .offset(values.offset)
        )
        async with self.db._engine.connect() as conn:
            rows = [dict(row) for row in (await conn.execute(stmt)).mappings()]
        return {"success": True, "waits": rows, "count": len(rows)}

    async def _wait_v2_cancel(self, args):
        if not self._wait_v2_enabled():
            return refusal("wait.disabled", "verified wait writes are disabled")
        filters = await self._wait_v2_read_filter(args)
        scope = await self._wait_v2_authorize_target(
            args,
            holder=(current_principal().kind == PrincipalKind.SESSION and "owner_id" in filters),
        )
        return await self._wait_v2_invoke("wait_cancel", args, scope=scope)

    async def _wait_v2_read_filter(self, args):
        principal = current_principal()
        if principal.kind == PrincipalKind.PLAYBOOK:
            if not principal.project_id or not principal.parent_run_id:
                raise WaitError("wait.out_of_scope", "playbook has no bound run")
            if (
                args.get("project_id", principal.project_id) != principal.project_id
                or args.get("session_id")
                or args.get("task_id")
            ):
                raise WaitError("wait.out_of_scope", "playbook owner scope cannot be nominated")
            return {
                "project_id": principal.project_id,
                "owner_kind": "playbook",
                "owner_id": principal.parent_run_id,
            }
        try:
            return await self._wait_read_filter(args)
        except LegacyWaitError as exc:
            raise WaitError("wait.out_of_scope", str(exc)) from exc

    async def _wait_v2_authorize_target(self, args, *, holder=False, filters=None):
        """Read authorization is independent of v2 activation and hook availability."""
        principal = current_principal()
        if principal.kind == PrincipalKind.SERVICE:
            return None
        if filters is None:
            filters = await self._wait_v2_read_filter(args)
        record = await self._wait_v2_row(args["wait_id"]) if args.get("wait_id") else None
        if args.get("wait_id") and record is None:
            raise WaitError("wait.source_unresolved", "wait not found")
        if record and any(
            value is not None and record[key] != value for key, value in filters.items()
        ):
            raise WaitError("wait.out_of_scope", "wait belongs to another owner or project")
        if args.get("task_id"):
            task = await self.db.get_task(args["task_id"])
            if not task or (filters.get("project_id") and task.project_id != filters["project_id"]):
                raise WaitError("wait.out_of_scope", "task belongs to another project")
            if filters.get("owner_id") and args["task_id"] != filters["owner_id"]:
                raise WaitError("wait.out_of_scope", "task belongs to another owner")
        if holder:
            scope = await self._wait_v2_scope(args, mutation=True)
            if record and (
                record["session_id"] != scope.session_id
                or (scope.claim_epoch is not None and record["claim_epoch"] != scope.claim_epoch)
            ):
                raise WaitError("continuation.stale_generation", "wait holder changed")
            return scope
        return filters

    async def _wait_v2_invoke(self, name, args, *, scope=None):
        handlers = getattr(self.orchestrator, "wait_v2_handlers", None)
        callback = handlers.get(name) if isinstance(handlers, Mapping) else None
        if callback is None:
            return refusal("wait.unavailable", f"{name} implementation is not installed")
        result = await callback(args, principal=current_principal(), scope=scope, handler=self)
        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            raise TypeError("wait handler must return a success dictionary")
        return result

    async def _wait_v2_command(self, name, args):
        principal = current_principal()
        if not caller_allowed(name, principal):
            return refusal("wait.out_of_scope", f"caller cannot execute {name}")
        try:
            values = WAIT_V2_ARGS[name].model_validate(args)
            params = values.model_dump(exclude_none=True)
            grant = operator_wait_grant(name, principal)
            if grant is not None:
                task, session = await self._operator_identity()
                if (
                    task.id != grant.task_id
                    or task.project_id != grant.project_id
                    or task.claim_epoch != grant.claim_epoch
                    or session.id != grant.session_id
                    or params.get("project_id", grant.project_id) != grant.project_id
                ):
                    raise WaitError("wait.out_of_scope", "operator wait scope changed")
                params["project_id"] = grant.project_id
                if name == "wait_graph":
                    if values.fleet:
                        raise WaitError(
                            "wait.out_of_scope", "operator wait reads are project-scoped"
                        )
                    scope = {"project_id": grant.project_id}
                else:
                    scope = await self._wait_v2_authorize_target(
                        params, filters={"project_id": grant.project_id}
                    )
            elif name in SUPERVISOR_WAIT_COMMANDS and principal.kind == PrincipalKind.SESSION:
                session = await self.db.get_session(principal.session_id)
                if (
                    not session
                    or session.profile_id != "supervisor"
                    or session.lifecycle != "named"
                ):
                    raise WaitError("wait.out_of_scope", "a named supervisor is required")
                if session.instance_token != principal.session_instance_token:
                    raise WaitError("wait.out_of_scope", "supervisor instance changed")
                if session.project_id != principal.project_id or session.state not in {
                    "starting",
                    "running",
                }:
                    raise WaitError("wait.out_of_scope", "supervisor scope is not live")
                if (
                    principal.project_id
                    and params.get("project_id", principal.project_id) != principal.project_id
                ):
                    raise WaitError("wait.out_of_scope", "supervisor project mismatch")
                if name == "wait_graph" and values.fleet:
                    if not principal.policy.allows("aq_commands", "waits.fleet_summary"):
                        return refusal(
                            "capability_denied", "capability denied: waits.fleet_summary"
                        )
                    scope = {"fleet": True}
                elif name == "wait_graph":
                    project = principal.project_id or params.get("project_id")
                    if not project:
                        raise WaitError("wait.out_of_scope", "select a project or fleet")
                    scope = {"project_id": project}
                    params["project_id"] = project
                else:
                    scope = await self._wait_v2_authorize_target(params)
            elif name in SERVICE_WAIT_COMMANDS or name == "wait_legacy_bridge":
                scope = None
            elif name == "wait_graph":
                scope = {"project_id": values.project_id, "fleet": values.fleet}
            else:
                scope = await self._wait_v2_authorize_target(
                    params, holder=name in {"wait_checkpoint", "wait_ack"}
                )
            if (
                name not in {"wait_explain", "wait_graph", "wait_legacy_bridge"}
                and not self._wait_v2_enabled()
            ):
                return refusal("wait.disabled", "verified wait writes are disabled")
            return await self._wait_v2_invoke(name, params, scope=scope)
        except (WaitError, LegacyWaitError) as exc:
            code = "wait.out_of_scope" if exc.code == "out_of_scope" else exc.code
            return refusal(code, exc, **getattr(exc, "details", {}))
        except (ValidationError, ValueError, TypeError) as exc:
            return refusal(
                "wait.checkpoint_invalid" if name == "wait_checkpoint" else "wait.invalid", exc
            )

    async def _cmd_wait_explain(self, args):
        return await self._wait_v2_command("wait_explain", args)

    async def _cmd_wait_graph(self, args):
        return await self._wait_v2_command("wait_graph", args)

    async def _cmd_wait_checkpoint(self, args):
        return await self._wait_v2_command("wait_checkpoint", args)

    async def _cmd_wait_ack(self, args):
        return await self._wait_v2_command("wait_ack", args)

    async def _cmd_wait_decide(self, args):
        return await self._wait_v2_command("wait_decide", args)

    async def _cmd_wait_lift(self, args):
        return await self._wait_v2_command("wait_lift", args)

    async def _cmd_wait_source_commit(self, args):
        return await self._wait_v2_command("wait_source_commit", args)

    async def _cmd_wait_deadline_fire(self, args):
        return await self._wait_v2_command("wait_deadline_fire", args)

    async def _cmd_wait_resume_dispatch(self, args):
        return await self._wait_v2_command("wait_resume_dispatch", args)

    async def _cmd_wait_sleep_decide(self, args):
        return await self._wait_v2_command("wait_sleep_decide", args)

    async def _cmd_wait_evict(self, args):
        return await self._wait_v2_command("wait_evict", args)

    async def _cmd_scheduler_wait_sync(self, args):
        return await self._wait_v2_command("scheduler_wait_sync", args)

    async def _cmd_wait_legacy_bridge(self, args):
        return await self._wait_v2_command("wait_legacy_bridge", args)
