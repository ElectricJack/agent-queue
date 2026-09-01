"""Surface commands mixin — the agent-facing context and schema surface.

Phase S0 (docs/specs/implementation/aq-surface.md §9) filled in the output
contract slice: ``get_schema`` (backs ``aq schema``) and the ``task_show`` /
``task_set`` pair (back ``aq task show|set|details``).  Phase S1 adds
``prime`` (backs ``aq prime``) and ``task_handoff`` (backs ``aq handoff``).
``task_close`` / ``task_heartbeat`` (session-runtime, ``src/commands/
session_commands.py``), ``message_send`` / ``message_inbox`` (supervisor-
agent, ``src/commands/message_commands.py``), and ``ask_human``
(unscheduled in this spec's phase checklist beyond the §3 inventory table)
are not implemented here yet.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes a
flat ``dict`` of arguments and returns a ``dict`` — domain data on success,
``{"error": "..."}`` on failure.  ``CommandHandler.execute`` returns this
dict verbatim (no implicit ``"success"`` key is injected at that layer); the
wire-level ``{"ok": bool, "result"|"error"}`` shape is added by
``/api/execute`` (``src/api/execute.py``).  ``prime`` and ``task_handoff``
are exceptions: their §3 spec table explicitly documents a ``"success"`` key
in the returned shape, so they include it to match.
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)


class SurfaceCommandsMixin:
    """Surface command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # get_schema — backs `aq schema` (design §4.3)
    # ------------------------------------------------------------------

    async def _cmd_get_schema(self, args: dict) -> dict:
        """Return the system's enum catalog so agents never guess magic strings.

        Introspects the enums that exist in the codebase today.  Enums owned
        by subsystems that haven't landed yet (gate lifecycle beyond type/
        status, outcome/failure_class/work_outcome, session states — see
        design §4.3) are intentionally omitted rather than hard-coded here;
        they will appear automatically once those subsystems add their
        constants and this method is extended to read them.
        """
        from src.commands.session_commands import VALID_OUTCOMES
        from src.database.queries.session_queries import _SESSION_TRANSITIONS
        from src.database.tables import GATE_STATUSES, GATE_TYPES, TASK_DEP_TYPES
        from src.models import AgentState, ClaimResult, CLAIM_PHASES, TaskStatus, TaskType

        return {
            "schema_version": 1,
            "enums": {
                "task_status": [s.value for s in TaskStatus],
                "task_type": [t.value for t in TaskType],
                "dependency_type": list(TASK_DEP_TYPES),
                "gate_type": list(GATE_TYPES),
                "gate_status": list(GATE_STATUSES),
                "hierarchy_error": [
                    "not_found",
                    "cross_project",
                    "cycle",
                    "depth",
                    "self_parent",
                    "container_closed",
                    "has_children",
                    "open_children",
                    "open_descendants",
                    "live_descendants",
                    "cycle_check_skipped",
                ],
                # Swarm work model — claims and pools (§10, §11).
                "claim_result": [r.value for r in ClaimResult],
                "claim_phase": list(CLAIM_PHASES),
                "lifecycle": ["task", "named", "pool"],
                "session_state": list(_SESSION_TRANSITIONS),
                "agent_state": [s.value for s in AgentState],
                "outcome": list(VALID_OUTCOMES),
            },
        }

    # ------------------------------------------------------------------
    # task_show — backs `aq task show|details` (design §3.1)
    # ------------------------------------------------------------------

    async def _cmd_task_show(self, args: dict) -> dict:
        """Full task detail in one round trip: fields + deps + context + labels.

        Composes the existing ``_cmd_get_task`` (fields, dependency
        visualization, subtasks) with ``task_context`` rows and
        ``task_labels``.  Gate/work-state sections called for in the full
        spec table (§3) are added once work-graph's query layer for those
        substrate-only tables (``gates``, ``task_gates``) lands.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}

        # ``_cmd_get_task`` applies ``_assert_task_in_scope`` itself; the
        # composed sections below must not run when it refused.
        info = await self._cmd_get_task({"task_id": task_id})
        if "error" in info:
            return info

        info["context"] = await self.db.get_task_contexts(task_id)
        info["labels"] = await self.db.get_task_labels(task_id)
        deps = await self._cmd_task_deps({"task_id": task_id})
        info["provenance"] = deps.get("provenance", [])

        parent = None
        parent_id = info.get("parent_task_id")
        if parent_id:
            p = await self.db.get_task(parent_id)
            if p:
                parent = {"id": p.id, "title": p.title, "status": p.status.value}
        info["parent"] = parent
        info["children"] = await self.db.get_children_summary(task_id)
        info["claimed_by"] = await self._claimed_by(task_id)
        return info

    async def _claimed_by(self, task_id: str) -> dict | None:
        """Who currently holds *task_id*, or ``None`` when nobody does.

        The three parts of a claim live in three places (spec §14): the
        holding session in ``task_metadata.claimed_by_session``, the agent in
        ``tasks.assigned_agent_id``, and the fence in ``tasks.claim_epoch``.
        A task counts as claimed when at least one of the session or agent is
        set — a released claim clears both, and ``claim_epoch`` alone is just
        the historical high-water mark.
        """
        task = await self.db.get_task(task_id)
        if task is None:
            return None
        session_id = await self.db.get_task_meta(task_id, "claimed_by_session")
        agent_id = task.assigned_agent_id
        if not session_id and not agent_id:
            return None
        return {
            "session_id": session_id,
            "agent_id": agent_id,
            "claim_epoch": getattr(task, "claim_epoch", 0),
        }

    # ------------------------------------------------------------------
    # task_set — backs `aq task set` (design §3.1)
    # ------------------------------------------------------------------

    async def _cmd_task_set(self, args: dict) -> dict:
        """Work-state contract writes. Never performs status transitions.

        Supported fields: ``description`` (optional ``expected_description`` CAS),
        ``branch``, ``pr_url``, ``work_dir``, ``note``,
        ``labels_add``, ``labels_remove``, ``meta``.

        ``work_dir`` is recorded as task metadata (``task_metadata`` key
        ``"work_dir"``) rather than a proper workspace binding — the
        workspaces-v2 / work-graph "work-state" model this field ultimately
        belongs to (design §3.1) hasn't landed a task-facing write path yet.
        This is a placeholder home for the value, not the final shape.
        """
        task_id = args.get("task_id")
        if not task_id:
            return {"error": "task_id is required"}

        if any(str(key).startswith("manual_pause") for key in (args.get("meta") or {})):
            return {"error": "manual_pause is reserved; use pause_task/resume_task."}

        if any(str(key).startswith("supervisor_recovery") for key in (args.get("meta") or {})):
            return {"error": "supervisor_recovery metadata is reserved; use task_recover."}

        # Validate the description contract before touching any legacy field.
        for field in ("description", "expected_description"):
            if field in args and not isinstance(args[field], str):
                return {"error": f"{field} must be a string"}
        if "expected_description" in args and "description" not in args:
            return {"error": "expected_description requires description"}
        scope = self._current_scope or {}
        err = await self._assert_session_owns(
            task_id,
            session_id=scope.get("session_id") if not scope.get("elevated") else None,
            claim_epoch=args.get("claim_epoch"),
        )
        if err:
            return err

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        scope_error = self._task_findings_scope_error(task)
        if scope_error:
            return scope_error
        fields_changed: list[str] = []
        if "description" in args:
            from src.database.queries.task_comment_queries import TaskFindingsConflict

            fence, error = await self._task_findings_write_fence(task, args)
            if error:
                return error
            try:
                await self.db.update_task_description(
                    task_id, args["description"],
                    expected_description=args.get("expected_description"), fence=fence,
                )
            except TaskFindingsConflict as error:
                return self._task_findings_conflict(error)
            fields_changed.append("description")

        updates: dict = {}
        if "branch" in args:
            updates["branch_name"] = args["branch"]
        if "pr_url" in args:
            updates["pr_url"] = args["pr_url"]
        if updates:
            await self.db.update_task(task_id, **updates)
            fields_changed.extend(updates.keys())

        if args.get("note"):
            await self.db.add_task_context(task_id, type="note", label="note", content=args["note"])
            fields_changed.append("note")

        # Labels are the work-graph's sanctioned free-text tag surface
        # (design §6); ``hold:<who>`` is the reserved convention that
        # withholds a task from the ready frontier.
        for label in args.get("labels_add") or []:
            await self.db.add_task_label(task_id, label)
            await self.db.log_event(
                "label.added", project_id=task.project_id, task_id=task_id, payload=label
            )
            fields_changed.append(f"+label:{label}")

        for label in args.get("labels_remove") or []:
            entered = await self.db.remove_task_label(task_id, label)
            await self.db.log_event(
                "label.removed", project_id=task.project_id, task_id=task_id, payload=label
            )
            await self.db._notify_ready([(t, "hold_removed") for t in entered])
            fields_changed.append(f"-label:{label}")

        if "work_dir" in args:
            await self.db.set_task_meta(task_id, "work_dir", args["work_dir"])
            fields_changed.append("work_dir")

        meta = args.get("meta") or {}
        for key, value in meta.items():
            await self.db.set_task_meta(task_id, key, value)
            fields_changed.append(f"meta:{key}")

        if not fields_changed:
            return {
                "error": (
                    "No fields to update. Provide description, branch, pr_url, work_dir, note, "
                    "labels_add, labels_remove, or meta."
                )
            }

        if "description" in fields_changed:
            await self._emit_task_findings_updated(task)
        result = await self._cmd_task_show({"task_id": task_id})
        result["fields_changed"] = fields_changed
        return result

    # ------------------------------------------------------------------
    # prime — backs `aq prime` (design §5, implementation §2-§3)
    # ------------------------------------------------------------------

    async def _cmd_prime(self, args: dict) -> dict:
        """Render the startup prime document for a task via ``src/prime/``.

        ``task_id`` resolution from the request's bearer-token scope
        (design §7) is Phase S2 territory and not implemented yet — this
        phase requires ``task_id`` explicitly.  The CLI (``aq prime``)
        forwards an ``AQ_TASK_ID`` env var as a stand-in for the future
        scope so callers don't need a flag once session-runtime starts
        setting that variable; the daemon side of that contract is S2's to
        build, this command only accepts whatever ``task_id`` it's given.
        """
        task_id = args.get("task_id")
        scope = getattr(self, "_current_scope", None) or {}
        if not task_id:
            task_id = scope.get("task_id")
        if not task_id and scope.get("session_id"):
            # Pool sessions have no fixed task in scope — the current claim
            # (``sessions.task_id``) is the only source of truth.
            s = await self.db.get_session(scope["session_id"])
            task_id = s.task_id if s else None
        if not task_id:
            return {
                "error": (
                    "task_id is required (no task in scope — pass task_id "
                    "explicitly or run inside a task session)"
                )
            }
        # A pool token pins no task, so ``check_command_scope`` cannot fence
        # an explicit ``--task-id``; the project fence is the one that holds.
        scope_err = self._assert_task_in_scope(await self.db.get_task(task_id))
        if scope_err:
            return scope_err

        from src.prime import PrimeRenderer

        # Prime is a genuine delivery path when messages are enabled: the
        # rendered pending-messages section marks each row delivered via
        # CAS with ``via="prime"`` so a subsequent nudge / inject cannot
        # double-deliver (supervisor-agent.md §8).
        messages_cfg = getattr(self.config, "messages", None)
        mark_messages_delivered = bool(getattr(messages_cfg, "enabled", False))

        renderer = PrimeRenderer(self.db, self.config)
        try:
            doc = await renderer.render_for_task(
                task_id,
                session_id=args.get("session_id"),
                work_dir=args.get("work_dir"),
                mark_messages_delivered=mark_messages_delivered,
            )
        except ValueError as exc:
            return {"error": str(exc)}

        return {
            "success": True,
            "body": doc.to_markdown(),
            "sections": [{"key": s.key, "title": s.title, "body": s.body} for s in doc.sections],
            "source": doc.source,
            "tokens_est": doc.tokens_est(),
        }

    # ------------------------------------------------------------------
    # task_handoff — backs `aq handoff` (design §6.1, implementation §3)
    # ------------------------------------------------------------------

    async def _cmd_task_handoff(self, args: dict) -> dict:
        """Write a ``task_context(type=handoff)`` row; optionally request a restart.

        ``--auto`` (wired to ``PreCompact``) writes a note only, never a
        restart — restarting on every compaction loops forever (design
        §6.1). Non-auto writes the note **and** emits
        ``session.restart_requested`` on the event bus; session-runtime
        owns restart mechanics (recycle now vs. later, wake_mode) — this
        command only records intent.
        """
        task_id = args.get("task_id")
        scope = getattr(self, "_current_scope", None) or {}
        if not task_id:
            task_id = scope.get("task_id")
        if not task_id:
            return {
                "error": (
                    "task_id is required (no task in scope — pass task_id "
                    "explicitly or run inside a task session)"
                )
            }

        err = await self._assert_session_owns(
            task_id, session_id=scope.get("session_id"), claim_epoch=args.get("claim_epoch")
        )
        if err:
            return err

        task = await self.db.get_task(task_id)
        if not task:
            return {"error": f"Task '{task_id}' not found"}

        auto = bool(args.get("auto", False))
        session_id = args.get("session_id") or scope.get("session_id")
        payload = {
            "subject": args.get("subject") or "",
            "detail": args.get("detail") or "",
            "session_id": session_id,
            "auto": auto,
            "ts": time.time(),
        }
        handoff_id = await self.db.add_task_context(
            task_id, type="handoff", label="handoff", content=json.dumps(payload)
        )

        restart_requested = False
        if not auto:
            bus = getattr(self.orchestrator, "bus", None)
            if bus is not None:
                await bus.emit(
                    "session.restart_requested",
                    {
                        "task_id": task_id,
                        "session_id": session_id,
                        "reason": "handoff",
                        "handoff_id": handoff_id,
                    },
                )
            restart_requested = True

        return {
            "success": True,
            "handoff_id": handoff_id,
            "restart_requested": restart_requested,
        }

    # ------------------------------------------------------------------
    # subagent_event — backs `aq subagent event --hook-json`
    # ------------------------------------------------------------------

    async def _cmd_subagent_event(self, args: dict) -> dict:
        """Record one ``SubagentStart`` / ``SubagentStop`` from a harness hook.

        The session is taken from the bearer token's scope, never from the
        payload: the hook runs inside the session's own process tree with
        that session's ``AQ_API_TOKEN``, and a session must not be able to
        write another session's telemetry by naming it.  A local (untokened)
        caller may pass ``session_id`` explicitly — that is the test and
        replay path.

        Duplicate deliveries collapse onto the row they already wrote
        (``subagent_event_id``), and a ``stop`` with no matching ``start`` is
        stored rather than rejected: the fold clamps at zero, so a lost Start
        under-counts for a moment instead of pinning a session at "one child
        running" forever.
        """
        event = str(args.get("event") or "").strip().lower()
        if event not in {"start", "stop"}:
            return {"error": "event must be 'start' or 'stop'"}
        subagent_id = str(args.get("subagent_id") or "").strip()
        if not subagent_id:
            return {"error": "subagent_id is required"}

        scope = getattr(self, "_current_scope", None) or {}
        session_id = scope.get("session_id") or args.get("session_id")
        if not session_id:
            return {"error": "session_id is required (no session in scope)"}
        session = await self.db.get_session(str(session_id))
        if session is None:
            return {"error": f"No session '{session_id}'"}

        recorded = await self.db.record_subagent_event(
            session_id=session.id,
            harness=session.harness,
            event=event,
            subagent_id=subagent_id,
            project_id=session.project_id,
            task_id=session.task_id,
            agent_type=(str(args.get("agent_type")).strip() or None)
            if args.get("agent_type") else None,
            turn_id=(str(args.get("turn_id")).strip() or None)
            if args.get("turn_id") else None,
        )
        counts = (await self.db.subagent_counts_by_session([session.id])).get(
            session.id, {"starts": 0, "stops": 0}
        )
        return {
            "success": True,
            "session_id": session.id,
            "event": event,
            "subagent_id": subagent_id,
            # False means "already had this one" — the hook fired twice, not
            # an error the harness should surface to the agent.
            "recorded": recorded,
            "active_subagent_count": max(0, counts["starts"] - counts["stops"]),
            "subagents_spawned_total": counts["starts"],
        }
