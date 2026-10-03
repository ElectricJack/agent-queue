"""Handlers for the consolidated integration operator surface (spec §5.1).

Four human decisions and two diagnostics remain once the reconciler owns the
mechanics: ``gate answer``, ``authorize`` (``integration_authorize_root``),
``policy activate``, ``hold``, ``status`` and ``explain``; ``flush`` survives as
"make it due now".  Every one is admitted for the local operator and a live
named supervisor of the project alike (:func:`integration_operator`).  The
older controls stay callable behind ``aq integration legacy``; see
:mod:`src.commands.integration_legacy`.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.commands.supervisor_authority import integration_operator

INTEGRATION_GATE_PREFIX = "integration-subject:"

#: Subject columns every operator view shows (status, explain, doctor).
_SUBJECT_VIEW_FIELDS = (
    "id", "project_id", "kind", "engine", "phase", "task_id", "batch_id", "target_ref",
    "head_sha", "generation", "next_due_at", "max_wait_seconds", "wait_reason", "gate_id",
    "refusal_streak", "last_visit_at", "last_journal_seq", "closed_reason", "writer_status",
    "writer_task_id", "budget_class", "budget_attempts", "budget_attempt_limit",
    "budget_deadline_at", "policy_playbook_id", "policy_artifact_sha256",
)
_JOURNAL_VIEW_FIELDS = (
    "seq", "entry_kind", "rule", "primitive", "outcome", "phase", "head_sha", "generation",
    "mode", "recorded_at", "payload",
)


def _failure(outcome: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "outcome": outcome, "error": error, **extra}


def integration_gate_subject(await_id: str) -> tuple[str, bool] | None:
    """``(subject_id, journaled)`` for an integration gate's ``await_id``.

    A gate the engine opens itself (``GatePrimitives.gate``) is keyed by the
    subject alone and its answer is journaled; the root adapter's gate adds
    ``:<request digest>`` and its answer is the gate row's resolution.
    Subject ids never contain ``:``.
    """
    if not await_id.startswith(INTEGRATION_GATE_PREFIX):
        return None
    subject_id, separator, _ = await_id.removeprefix(INTEGRATION_GATE_PREFIX).partition(":")
    return (subject_id, not separator) if subject_id else None


def _row_gate_choices(gate: dict[str, Any]) -> tuple[str, ...]:
    """The choices the root adapter wrote into its gate question."""
    _, separator, tail = str(gate.get("question") or "").rpartition("\nChoices: ")
    return tuple(c.strip() for c in tail.split(",") if c.strip()) if separator else ()


async def answer_integration_gate(
    db, gate: dict[str, Any], subject_row: dict[str, Any], *, choice: str, answered_by: str,
    resolve=None,
) -> tuple[str, str | None]:
    """Answer one integration gate as a verified human: ``(outcome, reason)``.

    The caller has established that *answered_by* is a verified human.
    *resolve* resolves a plain gate row with the orchestrator's events
    (``_resolve_gate_and_emit``); without one the row is resolved directly.
    """
    from src.integration.gates import GatePrimitives
    from src.integration.subjects import Subject

    _, journaled = integration_gate_subject(str(gate.get("await_id") or "")) or ("", True)
    gate_id = str(gate["id"])
    if journaled:
        result = await GatePrimitives(db).answer(
            Subject.from_row(subject_row), gate_id, choice=choice, answered_by=answered_by,
            verified_human=True,
        )
        return result.outcome, result.reason
    # The root adapter reads the gate row's resolution on its next visit.
    if subject_row.get("gate_id") != gate_id:
        return "unknown", "gate_not_current"
    if gate.get("status") != "open":
        if gate.get("resolution") == choice:
            return "answered", None
        return "unknown", "answer_immutable"
    now = time.time()
    if gate.get("timeout_at") is not None and now >= float(gate["timeout_at"]):
        return "unknown", "approval_expired"
    if choice not in _row_gate_choices(gate):
        return "unknown", "invalid_gate_answer"
    if resolve is not None:
        await resolve(gate_id, resolved_by=answered_by, resolution=choice)
    else:
        await db.resolve_gate(gate_id, resolved_by=answered_by, resolution=choice)
    await db.wake_integration_subjects(now=now, subject_ids=[str(subject_row["id"])])
    return "answered", None


def subject_view(row: dict[str, Any], *, now: float) -> dict[str, Any]:
    """The operator's view of one subject: where it is, why, and when it is due."""
    view = {key: row.get(key) for key in _SUBJECT_VIEW_FIELDS}
    due = row.get("next_due_at")
    view["overdue_seconds"] = (
        round(now - float(due), 3) if due is not None and float(due) < now else 0.0
    )
    return view


async def integration_subject_views(
    db, project_id: str, *, subject_ids: tuple[str, ...] = (), include_done: bool = False,
) -> list[dict[str, Any]]:
    """Each subject's view, with its open gate's question and choices."""
    from src.integration.records import journal_entry_on

    rows = await db.list_integration_subjects(
        project_id=project_id, subject_ids=subject_ids, include_done=include_done,
    )
    now = time.time()
    views = [subject_view(row, now=now) for row in rows]
    gated = [view for view in views if view["gate_id"]]
    if gated:
        async with db._engine.connect() as conn:
            for view in gated:
                entry = await journal_entry_on(conn, view["id"], f"gate:{view['gate_id']}")
                payload = (entry or {}).get("payload") or {}
                request = payload.get("request") or {}
                view["gate"] = {
                    "gate_id": view["gate_id"],
                    "question": request.get("question"),
                    "choices": list(request.get("choices") or ()),
                    "default_choice": request.get("default_choice"),
                    "timeout_at": payload.get("timeout_at"),
                    "opened_at": (entry or {}).get("recorded_at"),
                }
    return views


async def attach_status_subjects(
    db, result: dict[str, Any], project_id: str, subject_id: str | None
) -> dict[str, Any]:
    """Attach the observer's subject view to ``integration status``."""
    if result.get("outcome") == "not_found":
        return result
    views = await integration_subject_views(
        db, project_id, subject_ids=(subject_id,) if subject_id else (),
        include_done=bool(subject_id),
    )
    if subject_id and not views:
        return _failure("not_found", f"subject {subject_id} is not in project {project_id}")
    return {**result, "subjects": views}


async def wake_project_subjects(db, project_id: str) -> int:
    """Make every live subject of *project_id* due now (§5.1 ``flush``)."""
    return await db.wake_integration_subjects(now=time.time(), project_ids=[project_id])


def gate_resolver(handler: Any):
    """The orchestrator's resolve-and-emit hook, or ``None`` without one."""
    return getattr(getattr(handler, "orchestrator", None), "_resolve_gate_and_emit", None)


class IntegrationSurfaceCommandsMixin:
    """``gate answer``, ``policy activate``, ``hold``, ``explain`` and status subjects."""

    async def _integration_scope_refusal(
        self, project_id: str, args: dict
    ) -> tuple[str | None, dict | None]:
        """``(operator_id, refusal)`` for a control on *project_id*."""
        claimed = args.get("project_id")
        if claimed and str(claimed) != project_id:
            return None, _failure("unauthorized", "the target belongs to another project")
        operator_id, refusal = await integration_operator(getattr(self, "db", None), project_id)
        if refusal is not None:
            return None, _failure("unauthorized", refusal)
        return operator_id, None

    async def _cmd_integration_gate_answer(self, args: dict) -> dict:
        gate_id = str(args.get("gate_id") or "")
        choice = str(args.get("choice") or "")
        gate = await self.db.get_gate(gate_id) if gate_id else None
        identity = integration_gate_subject(str((gate or {}).get("await_id") or ""))
        if gate is None or identity is None:
            return _failure("not_found", f"no integration gate {gate_id!r}", gate_id=gate_id)
        subject_id = identity[0]
        row = await self.db.get_integration_subject(subject_id)
        if row is None:
            return _failure("not_found", "the gate's integration subject is missing",
                            gate_id=gate_id, subject_id=subject_id)
        project_id = str(row["project_id"])
        operator_id, refusal = await self._integration_scope_refusal(project_id, args)
        if refusal is not None:
            return refusal
        identity = {"gate_id": gate_id, "subject_id": subject_id, "project_id": project_id,
                    "choice": choice}
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            # A gate is a human decision (§3.7): only a verified human's answer
            # binds, so a supervisor relays the question rather than answering it.
            return _failure(
                "refused",
                "verified_human_required: a gate binds only the local operator's answer; "
                f"ask the operator to run `aq integration gate answer {gate_id} <choice>`",
                reason="verified_human_required", **identity,
            )
        outcome, reason = await answer_integration_gate(
            self.db, gate, row, choice=choice, answered_by=operator_id,
            resolve=gate_resolver(self),
        )
        if outcome != "answered":
            reason = reason or outcome
            return _failure("refused", f"gate answer refused: {reason}", reason=reason,
                            **identity)
        return {"success": True, "outcome": "answered", "answered_by": operator_id, **identity}

    async def _cmd_integration_policy_activate(self, args: dict) -> dict:
        project_id = str(args.get("project_id") or "")
        operator_id, refusal = await self._integration_scope_refusal(project_id, args)
        if refusal is not None:
            return refusal
        mode = str(args.get("mode") or "")
        reason = str(args.get("reason") or "")
        policy = args.get("policy")
        if mode == "development":
            result = await self._cmd_integration_develop(
                {"project_id": project_id, "policy": policy, "reason": reason}
            )
            if result.get("outcome") not in {"configured", "blocked", "unauthorized"}:
                return _failure("blocked", str(result.get("error") or result.get("outcome")))
            return result
        controls = self._integration_control_service()
        generation = int(args["expected_generation"])
        configured: dict[str, Any] = {}
        try:
            if policy is not None:
                result = await controls.configure(
                    project_id,
                    updates={"hierarchical_integration_policy": policy},
                    expected_generation=generation,
                    reason=reason,
                    operator_id=operator_id,
                )
                if result.get("outcome") != "configured":
                    return {"success": False, **result}
                generation = int(result["generation"])
                configured = {"fields": ["hierarchical_integration_policy"],
                              "configured_generation": generation}
                if mode == "disabled":
                    return {"success": True, **result, **configured}
            result = await controls.enable(
                project_id,
                mode=mode,
                expected_generation=generation,
                reason=reason,
                operator_id=operator_id,
                waiver_id=args.get("waiver_id"),
                interval_seconds=args.get("interval_seconds"),
            )
        except ValueError as exc:
            return _failure("blocked", str(exc), project_id=project_id, **configured)
        return {**result, **configured}

    async def _integration_hold_tasks(self, target: str) -> tuple[dict | None, dict]:
        """``(refusal, resolved)`` for a hold target: a task or a task's subject."""
        subject = await self.db.get_integration_subject(target)
        if subject is not None:
            if not subject.get("task_id"):
                return _failure(
                    "invalid",
                    f"subject {target} is a root batch with no task of its own; hold one of "
                    "its member tasks, or pause the project",
                    target=target,
                ), {}
            return None, {"project_id": str(subject["project_id"]), "subject_id": target,
                          "task_id": str(subject["task_id"])}
        task = await self.db.get_task(target)
        if task is None:
            return _failure("not_found", f"no integration subject or task {target!r}",
                            target=target), {}
        return None, {"project_id": str(task.project_id), "subject_id": None,
                      "task_id": task.id}

    async def _cmd_integration_hold(self, args: dict) -> dict:
        from src.integration.subjects import OPERATOR_HOLD_META_KEY

        target = str(args.get("target") or "")
        refusal, resolved = await self._integration_hold_tasks(target)
        if refusal is not None:
            return refusal
        project_id, task_id = resolved["project_id"], resolved["task_id"]
        operator_id, refusal = await self._integration_scope_refusal(project_id, args)
        if refusal is not None:
            return refusal
        identity = {"target": target, "project_id": project_id,
                    "subject_id": resolved["subject_id"], "task_ids": [task_id]}
        existing = await self.db.get_task_meta(task_id, OPERATOR_HOLD_META_KEY)
        now = time.time()
        if args.get("release"):
            if existing is None:
                return {"success": True, "outcome": "not_held", **identity}
            await self.db.delete_task_meta(task_id, OPERATOR_HOLD_META_KEY)
            outcome, reason = "released", (existing or {}).get("reason")
        elif existing is not None:
            return {"success": True, "outcome": "already_held", **identity,
                    "reason": existing.get("reason"), "held_by": existing.get("held_by")}
        else:
            reason = str(args.get("reason") or "").strip()
            await self.db.set_task_meta(
                task_id, OPERATOR_HOLD_META_KEY,
                {"reason": reason, "held_by": operator_id, "held_at": now},
            )
            outcome = "held"
        # The hold is an observed fact; waking makes the reconciler see it now.
        woken = await self.db.wake_integration_subjects(
            now=now, task_ids=[task_id], writer_task_ids=[task_id],
        )
        # A root's observer reads its member tasks' holds, so roots look again too.
        roots = [
            row["id"] for row in await self.db.list_integration_subjects(
                project_id=project_id, roots_only=True, limit=1000,
            )
        ]
        if roots:
            woken += await self.db.wake_integration_subjects(now=now, subject_ids=roots)
        await self.db.log_event(
            f"integration.hold_{outcome}", project_id=project_id, task_id=task_id,
            payload=json.dumps({"operator": operator_id, "target": target}),
        )
        return {"success": True, "outcome": outcome, **identity, "reason": reason,
                "held_by": operator_id, "woken_subjects": woken}

    async def _cmd_integration_explain(self, args: dict) -> dict:
        target = str(args.get("target") or "")
        limit = int(args.get("limit") or 5)
        subject = await self.db.get_integration_subject(target)
        if subject is not None:
            project_id = str(subject["project_id"])
            subject_ids: tuple[str, ...] = (target,)
        else:
            task = await self.db.get_task(target)
            if task is None:
                return _failure("not_found", f"no integration subject or task {target!r}",
                                target=target)
            project_id = str(task.project_id)
            subject_ids = tuple(
                row["id"]
                for row in await self.db.list_integration_subjects(
                    project_id=project_id, task_ids=(target,), include_done=True
                )
            )
            if not subject_ids:
                return _failure("not_found", f"task {target} has no integration subject",
                                target=target, project_id=project_id)
        _operator, refusal = await self._integration_scope_refusal(project_id, args)
        if refusal is not None:
            return refusal
        views = await integration_subject_views(
            self.db,
            project_id, subject_ids=subject_ids, include_done=True
        )
        for view in views:
            entries = await self.db.list_integration_subject_journal(
                view["id"], limit=limit, newest_first=True
            )
            view["decisions"] = [
                {key: entry.get(key) for key in _JOURNAL_VIEW_FIELDS} for entry in entries
            ]
        return {"success": True, "outcome": "explained", "target": target,
                "project_id": project_id, "subjects": views}
