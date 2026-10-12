"""Session and task-close commands — the completion protocol's daemon side.

Two families live here:

``_cmd_session_*``
    Operator surface: list, show, peek, attach, nudge, logs, kill.  Plus
    ``session_drain_ack``, which is *agent*-facing — the second half of the
    completion protocol.

``_cmd_task_close`` / ``_cmd_task_heartbeat``
    The agent declaring what happened.  ``task_close`` is the **only** way a
    session-run task reaches COMPLETED: process exit is a failure signal,
    never a success signal, so an agent that does not say it finished did
    not finish.  ``task_heartbeat`` refreshes the lease so the stall ladder
    does not start climbing during a long but legitimate command.

Convention (see ``src/commands/handler.py``): every ``_cmd_*`` method takes
a flat ``dict`` of arguments and returns a ``dict`` — domain data on
success, ``{"error": "..."}`` on failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path

from src.claim_file import remove_claim_file, remove_claim_file_if_matches
from src.database.queries.result_queries import (
    PENDING_COMPLETION_KEY,
    close_identity,
    completion_draft,
)
from src.database.queries.task_queries import StaleClaim
from src.database.queries.task_subtask_queries import OPEN_SUBTASK_STATUSES, SubtasksOpenError
from src.models import TaskCompletion, TaskStatus
from src.sessions.provider import (
    Cap,
    CapabilityUnsupported,
    NotSubmitted,
    SessionHandle,
)
# Safe at module scope: the reconciler's claim-file helpers live in the leaf
# module ``src.claim_file``, so it no longer imports this package back.
from src.sessions.reconciler import DRAIN_ACK_KEY, LIVE_SESSION_STATES

logger = logging.getLogger(__name__)

#: Close verdict vocabulary.  Mirrors the typed schemas in
#: ``src/tools/definitions.py`` so the CLI, MCP and HTTP surfaces reject the
#: same set; the *meaning* of each value is owned by the work-graph spec's
#: outcome-metadata contract (design §7).
VALID_OUTCOMES = ("pass", "fail")
VALID_FAILURE_CLASSES = ("transient", "hard")
VALID_WORK_OUTCOMES = ("shipped", "no-op", "blocked", "abandoned")

#: Statuses a task may be closed from.  ASSIGNED is accepted because a fast
#: agent can call close before the orchestrator's own IN_PROGRESS write has
#: landed; refusing that would make the protocol racy for no benefit.
_CLOSEABLE = (TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED)


class SessionCommandsMixin:
    """Session command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _provider_for_session(self, session):
        registry = getattr(self.orchestrator, "session_providers", None)
        if registry is None:
            return None
        try:
            return registry.create(session.provider, self.config)
        except ValueError:
            return None

    @staticmethod
    def _session_handle(session) -> SessionHandle:
        return SessionHandle(
            name=session.name,
            provider=session.provider,
            instance_token=session.instance_token,
        )

    #: A shorter prefix is too likely to be a typo that happens to match.
    _MIN_ID_PREFIX = 4

    async def _session_by_id_prefix(self, prefix: str):
        """``(session, [])`` for a unique id prefix, ``(None, ids)`` when ambiguous."""
        if len(prefix) < self._MIN_ID_PREFIX:
            return None, []
        matches = [row for row in await self.db.list_sessions() if row.id.startswith(prefix)]
        if len(matches) == 1:
            return matches[0], []
        return None, [row.id for row in matches] if len(matches) > 1 else []

    async def _resolve_session(self, args: dict):
        """Resolve a session from ``session_id``, ``name`` or ``task_id``.

        Returns ``(session, error_dict)``.  Names accepted here are
        *provider* names because this is the operator surface and they are
        what ``aq session list`` prints — other layers address named
        sessions by logical name and never construct provider names.
        """
        session_id = args.get("session_id") or args.get("id")
        if session_id:
            session = await self.db.get_session(str(session_id))
            if session is None:
                # Operators paste names as often as ids; try both before
                # failing, because "not found" for a name they can see in
                # `aq session list` is a confusing error.
                session = await self.db.get_session_by_name(str(session_id))
            if session is None:
                # `aq session list` prints the first 8 characters of each id,
                # and an operator pastes what they can see: accept a prefix,
                # but only when it names exactly one session.
                session, ambiguous = await self._session_by_id_prefix(str(session_id))
                if ambiguous:
                    return None, {
                        "error": (
                            f"'{session_id}' matches {len(ambiguous)} sessions "
                            f"({', '.join(sorted(ambiguous))}); use more of the id"
                        )
                    }
            if session is None:
                return None, {"error": f"No session '{session_id}'"}
            return session, None

        name = args.get("name")
        if name:
            session = await self.db.get_session_by_name(str(name))
            if session is None:
                return None, {"error": f"No session named '{name}'"}
            return session, None

        task_id = args.get("task_id")
        if task_id:
            session = await self.db.get_session_for_task(str(task_id))
            if session is None:
                return None, {"error": f"No session for task '{task_id}'"}
            return session, None

        return None, {"error": "session_id, name, or task_id is required"}

    def _session_project_scope_error(self, session) -> dict | None:
        """Refuse a session row belonging to another project than the caller's.

        A **per-project** elevated token is trusted for its own project and no
        further.  ``check_command_scope`` pins ``args["project_id"]`` for such a
        caller, but these commands address a row by id, unique id prefix, name
        or task and never read that field, so the pin is vacuous here and the
        fence has to live in the command.  A supervisor of project A could
        otherwise stop, read or drain-ack project B's workers by naming their
        session -- and the names are short prefixes printed by
        ``aq session list``.

        Local callers (the loopback CLI) and the global supervisor carry no
        project pin, so both stay unrestricted, as does any caller with no
        scope at all.  Same answer for every way :meth:`_resolve_session` can
        name a row, because the fence runs on the resolved session.
        """
        scope_project = (self._current_scope or {}).get("project_id")
        if scope_project is not None and session.project_id != scope_project:
            return {"success": False, "error": "out of scope: session belongs to another project"}
        return None

    @staticmethod
    def _session_dict(session) -> dict:
        return {
            "id": session.id,
            "name": session.name,
            "task_id": session.task_id,
            "agent_id": session.agent_id,
            "model": session.model,
            "intelligence_class": session.intelligence_class,
            "ended_at": session.ended_at,
            "end_reason": session.end_reason,
            "project_id": session.project_id,
            "profile_id": session.profile_id,
            "harness": session.harness,
            "provider": session.provider,
            "lifecycle": session.lifecycle,
            "state": session.state,
            "desired_state": session.desired_state,
            "work_dir": session.work_dir,
            "started_at": session.started_at,
            "last_activity": session.last_activity,
            "restarts": session.restarts,
            "claims": session.claims,
            "claim_phase": session.claim_phase,
            "claim_failure_count": session.claim_failure_count,
            "claim_failure_reason": session.claim_failure_reason,
            "claim_failure_at": session.claim_failure_at,
            "quarantined_at": session.quarantined_at,
            "sleep_reason": session.sleep_reason,
            "epoch": session.epoch,
        }

    # ------------------------------------------------------------------
    # operator surface
    # ------------------------------------------------------------------

    async def _cmd_supervisor_restart(self, args: dict) -> dict:
        from src.messages.session_lens import SupervisorRestartError

        if error := self._agent_settings_scope_error():
            return {"success": False, "error": error}
        name = args.get("name", "supervisor-global")
        resume = args.get("resume", False)
        session_id = args.get("session_id")
        if not isinstance(name, str) or not name.startswith("supervisor-") or name == "supervisor-":
            return {"success": False, "error": "name must be a supervisor messaging address"}
        if not isinstance(resume, bool):
            return {"success": False, "error": "resume must be a boolean"}
        if session_id is not None and (not isinstance(session_id, str) or not session_id):
            return {"success": False, "error": "session_id must be a non-empty string"}
        lens = getattr(self.orchestrator, "session_lens", None)
        if lens is None:
            return {"success": False, "error": "Supervisor session service is unavailable"}
        project_id = None if name == "supervisor-global" else name.removeprefix("supervisor-")
        try:
            session = await lens.restart_supervisor(
                target_id=name, project_id=project_id, resume=resume,
                expected_session_id=session_id,
            )
        except SupervisorRestartError as exc:
            return {"success": False, "error": str(exc)}
        await self.orchestrator.bus.emit("session.started", {
            "session_id": session.id, "name": session.name,
            "task_id": None, "project_id": session.project_id,
            "harness": session.harness, "provider": session.provider, "work_dir": session.work_dir,
        })
        return {
            "success": True, "session_id": session.id, "name": name,
            "state": session.state, "mode": "resume" if resume else "fresh",
        }

    async def _cmd_session_list(self, args: dict) -> dict:
        """List sessions with lifecycle, state, task, harness, activity."""
        limit = args.get("limit")
        offset = args.get("offset")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 500):
            return {"error": "limit must be an integer from 1 to 500"}
        if offset is None:
            offset = 0
        if type(offset) is not int or offset < 0:
            return {"error": "offset must be a non-negative integer"}
        sessions = await self.db.list_sessions(
            state=args.get("state"),
            desired_state=args.get("desired_state"),
            lifecycle=args.get("lifecycle"),
            project_id=args.get("project_id") or self._active_project_id,
            live_only=bool(args.get("live_only")),
            limit=limit + 1 if limit is not None else None,
            offset=offset,
        )
        has_more = limit is not None and len(sessions) > limit
        if has_more:
            sessions = sessions[:limit]
        now = time.time()
        ttl = self.config.sessions.lease_ttl_seconds
        rows = []
        for s in sessions:
            row = self._session_dict(s)
            # "stalled" is derived, never stored.  Computing it here keeps
            # the single definition (lease TTL vs last activity) in one
            # place for every surface that displays a session.
            last = s.last_activity or s.started_at
            row["idle_seconds"] = max(0.0, now - last)
            row["stalled"] = bool(s.state == "running" and ttl > 0 and (now - last) > ttl)
            rows.append(row)
        return {"success": True, "sessions": rows, "count": len(rows), "has_more": has_more}

    async def _cmd_session_show(self, args: dict) -> dict:
        """Full detail for one session."""
        session, err = await self._resolve_session(args)
        if err:
            return err
        return {"success": True, "session": self._session_dict(session)}

    async def _cmd_session_peek(self, args: dict) -> dict:
        """Last N lines of a session's visible output."""
        session, err = await self._resolve_session(args)
        if err:
            return err
        if (denied := self._session_project_scope_error(session)) is not None:
            return denied
        provider = self._provider_for_session(session)
        if provider is None:
            return {"error": f"Provider '{session.provider}' is not available"}
        if not provider.supports(Cap.PEEK):
            return {
                "success": True,
                "session_id": session.id,
                "output": "",
                "note": f"provider '{provider.name}' has no peek capability",
            }
        lines = int(args.get("lines") or args.get("n") or 60)
        try:
            output = await provider.peek(self._session_handle(session), lines)
        except Exception as exc:
            return {"error": f"peek failed: {exc}"}
        return {"success": True, "session_id": session.id, "output": output}

    async def _cmd_session_attach(self, args: dict) -> dict:
        """Return the shell command a human runs to attach."""
        session, err = await self._resolve_session(args)
        if err:
            return err
        provider = self._provider_for_session(session)
        if provider is None:
            return {"error": f"Provider '{session.provider}' is not available"}
        try:
            command = await provider.attach_command(self._session_handle(session))
        except CapabilityUnsupported as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"attach failed: {exc}"}
        return {"success": True, "session_id": session.id, "attach_command": command}

    async def _cmd_session_input(self, args: dict) -> dict:
        """Send direct human input; never queue a message or wake a session."""
        from src.sessions.provider import validate_terminal_input

        scope = self._current_scope
        if scope and scope.get("kind") != "local" and not (
            scope.get("elevated") and scope.get("project_id") is None
        ):
            return {"error": "out of scope: direct terminal input requires global admin"}
        text, key = args.get("text"), args.get("key")
        try:
            validate_terminal_input(text, key)
        except ValueError as exc:
            return {"error": str(exc)}
        session, err = await self._resolve_session(args)
        if err:
            return err
        if session.state not in {"running", "draining"}:
            return {"error": "No live terminal; start or resume the agent first"}
        if session.agent_id:
            agent = await self.db.get_agent(session.agent_id)
            if agent is None or agent.deleted_at is not None:
                return {"error": "This agent is no longer available"}
        provider = self._provider_for_session(session)
        if provider is None or not provider.supports(Cap.INPUT):
            return {"error": "This session provider does not support terminal input"}
        try:
            await provider.send_input(self._session_handle(session), text=text, key=key)
        except Exception:
            # Provider errors can include argv containing the user's text.
            # Neither return nor log it; failed input must not be auto-retried.
            return {"error": "Terminal input failed. Refresh the session before typing again."}
        await self.db.update_session(session.id, last_activity=time.time())
        return {"success": True, "session_id": session.id, "accepted": True}

    async def _cmd_session_nudge(self, args: dict) -> dict:
        """Inject text into a session and submit it.

        ``NotSubmitted`` is surfaced as a typed failure rather than
        swallowed: the caller has to know the text was pasted but never
        submitted, or it will assume a message was delivered that the agent
        never saw.
        """
        text = str(args.get("text") or args.get("message") or "").strip()
        if not text:
            return {"error": "text is required"}
        session, err = await self._resolve_session(args)
        if err:
            return err
        provider = self._provider_for_session(session)
        if provider is None:
            return {"error": f"Provider '{session.provider}' is not available"}
        try:
            await provider.nudge(self._session_handle(session), text)
        except NotSubmitted:
            return {"success": False, "error": "not_submitted", "session_id": session.id}
        except CapabilityUnsupported as exc:
            return {"success": False, "error": str(exc), "session_id": session.id}
        except Exception as exc:
            return {"error": f"nudge failed: {exc}"}
        return {"success": True, "session_id": session.id, "delivered": True}

    async def _cmd_session_logs(self, args: dict) -> dict:
        """Normalized output for a session.

        Transcript-sourced when a reader resolves for the session's
        harness *and* the on-disk transcript is present; otherwise falls
        back to the peek-diff tail.  Both paths label their source
        (``"transcript"`` / ``"peek"``) so the caller knows which layer
        the bytes came from — a silent switch is how an operator debugs
        the wrong thing.
        """
        from types import SimpleNamespace

        from src.api.auth import LOCAL_SCOPE, RequestScope
        from src.api.scope import check_command_scope
        from src.sessions.transcripts import resolve_reader

        attempt = None
        if args.get("attempt_id"):
            attempt = await self.db.get_task_session_attempt(str(args["attempt_id"]))
            if attempt is None:
                return {"error": "No such task session attempt"}
            args = {**args, "session_id": args.get("session_id") or args.get("id") or attempt["session_id"]}
        session, err = await self._resolve_session(args)
        if attempt is None and session is not None and session.task_id and session.state in {"stopped", "sleeping", "quarantined"}:
            history = await self.db.list_task_session_attempts(session.task_id)
            retained = next((item for item in history if item["session_id"] == session.id), None)
            if retained is not None:
                attempt = await self.db.get_task_session_attempt(retained["id"])
        if attempt is not None:
            if (
                (session is not None and session.id != attempt["session_id"])
                or (session is None and args["session_id"] != attempt["session_id"])
                or (args.get("task_id") and args["task_id"] != attempt["task_id"])
            ):
                return {"error": "Attempt does not belong to this session/task"}
            session = SimpleNamespace(**{
                **attempt, "id": attempt["session_id"],
                "started_at": attempt["session_started_at"],
            })
        elif err:
            return err
        scope = RequestScope(**self._current_scope) if self._current_scope else LOCAL_SCOPE
        if scope.kind != "local" and scope.project_id is not None and scope.project_id != session.project_id:
            return {"error": "Session project is out of scope"}
        denied = check_command_scope("session_logs", {
            "session_id": session.id, "project_id": session.project_id,
            "task_id": session.task_id,
        }, scope)
        if denied:
            return {"error": denied}

        # Default cap of 100 entries per call: rereading a large JSONL and
        # returning every entry over the CLI/MCP boundary produces multi-MB
        # payloads for long sessions.  Callers who want a bigger window
        # pass ``limit``/``lines``/``n`` explicitly.
        base_dir = getattr(self.orchestrator, "transcript_base_dir", None)
        reader = resolve_reader(session.harness, base_dir=base_dir)
        end = (attempt["ended_at"] or attempt.get("transcript_end_at")) if attempt else getattr(session, "ended_at", None)
        if session.state not in {"starting", "running", "draining"} and end is None:
            return {"success": True, "session_id": session.id, "source": "unavailable",
                    "entries": [], "note": "Legacy recording has no reliable end boundary."}
        if reader is not None:
            path = await asyncio.to_thread(reader.resolve_session, session)
            if path is not None:
                try:
                    # ``read_new`` runs the blocking file IO in
                    # ``asyncio.to_thread`` so a big transcript on a slow
                    # disk does not stall the event loop even though we
                    # read the whole file here before tailing.
                    entries, _ = await reader.read_new(path, 0)
                except Exception:
                    entries = []
                if attempt is not None:
                    fresh_attempt = await self.db.get_task_session_attempt(attempt["id"])
                    if fresh_attempt is None:
                        return {"success": True, "source": "unavailable", "entries": [],
                                "note": "This task attempt is no longer available."}
                    attempt = fresh_attempt
                    end = attempt["ended_at"] or attempt.get("transcript_end_at")
                    entries = [entry for entry in entries
                               if entry.ts >= attempt["started_at"]
                               and (end is None or (
                                   entry.ts <= end if attempt["ended_at"] is not None else entry.ts < end
                               ))]
                else:
                    fresh_session = await self.db.get_session(session.id)
                    if fresh_session is None:
                        return {"success": True, "source": "unavailable", "entries": [],
                                "note": "This session is no longer available."}
                    if fresh_session.ended_at is not None:
                        entries = [entry for entry in entries if 0 < entry.ts <= fresh_session.ended_at]
                if entries:
                    tail_size = int(args.get("limit") or args.get("lines") or args.get("n") or 100)
                    tail = entries[-tail_size:]
                    return {
                        "success": True,
                        "session_id": session.id,
                        "source": "transcript",
                        "entries": [
                            {
                                "uuid": e.uuid,
                                "parent_uuid": e.parent_uuid,
                                "type": e.type,
                                "text": e.text,
                                "model": e.model,
                                "usage": e.usage,
                                "ts": e.ts,
                            }
                            for e in tail
                        ],
                    }

        if attempt is not None or session.state not in {"starting", "running", "draining"}:
            return {
                "success": True, "session_id": session.id, "source": "unavailable",
                "entries": [], "note": "The recording for this ended session is unavailable.",
            }

        # Fallback: peek-diff.  Reuses the existing operator surface, then
        # relabels so the caller sees where the bytes came from.
        result = await self._cmd_session_peek(args)
        if "error" in result:
            return result
        result["source"] = "peek"
        return result

    async def _cmd_session_prune(self, args: dict) -> dict:
        """Remove one inactive named row; never signal or forget live work."""
        session, err = await self._resolve_session(args)
        if err:
            return err
        if (session.lifecycle != "named" or session.state not in {"sleeping", "stopped"}
                or session.task_id or session.claim_phase):
            return {"error": "Only sleeping/stopped named sessions without a task can be pruned"}
        provider = self._provider_for_session(session)
        if provider is None:
            return {"error": "Provider unavailable; cannot confirm session is inactive"}
        try:
            if not await provider.confirm_instance_stopped(self._session_handle(session)):
                return {"error": "Session still has a live terminal; stop it before pruning"}
            from src.sessions.proctable import scan_by_env_marker

            if any(process.marker == session.instance_token
                   for process in await scan_by_env_marker("AQ_INSTANCE_TOKEN")):
                return {"error": "Session still has live marked processes; stop them before pruning"}
        except Exception:
            return {"error": "Could not verify session is inactive"}
        changed = await self.db.update_session_instance(
            session.id, session.instance_token, require_desired_state=session.desired_state,
            desired_state="stopped",
        )
        if not changed:
            return {"error": "Session changed during cleanup; refresh the flock"}
        token_store = getattr(self.orchestrator, "token_store", None)
        if token_store is not None:
            await token_store.revoke_session(session.id)
        if not await self.db.prune_named_session(session.id, session.instance_token):
            return {"error": "Session changed during cleanup; refresh the flock"}
        await self.orchestrator.bus.emit("session.pruned", {
            "session_id": session.id, "project_id": session.project_id,
        })
        return {"success": True, "session_id": session.id}

    async def _cmd_session_cleanup(self, args: dict) -> dict:
        """Stop and forget taskless sleeping named sessions in one operator action."""
        project_id = (self._current_scope or {}).get("project_id")
        requested_project = args.get("project_id")
        if project_id is not None and requested_project not in (None, project_id):
            return {"error": "out of scope: project mismatch"}
        rows = await self.db.list_sessions(
            state="sleeping", lifecycle="named", project_id=project_id or requested_project,
        )
        candidates = [row for row in rows if row.desired_state in {"sleeping", "stopped"}
                      and row.task_id is None and row.claim_phase is None]
        if args.get("dry_run"):
            return {"success": True, "dry_run": True, "count": len(candidates),
                    "sessions": [self._session_dict(row) for row in candidates]}

        pruned = []
        skipped = []
        for row in candidates:
            # The instance and stop intent are fenced before touching the terminal.
            # A concurrent wake makes this update fail, leaving that session alone.
            current = await self.db.get_session(row.id)
            if (current is None or current.instance_token != row.instance_token
                    or current.state != "sleeping" or current.task_id is not None
                    or current.claim_phase is not None):
                skipped.append({"session_id": row.id, "reason": "session changed"})
                continue
            provider = self._provider_for_session(current)
            if provider is None:
                skipped.append({"session_id": row.id, "reason": "provider unavailable"})
                continue
            changed = await self.db.update_session_instance(
                row.id, row.instance_token, require_desired_state=current.desired_state,
                desired_state="stopped",
            )
            if not changed:
                skipped.append({"session_id": row.id, "reason": "session changed"})
                continue
            try:
                await provider.stop(self._session_handle(current), grace=2.0)
            except Exception as exc:  # noqa: BLE001 - report this row and continue the batch
                skipped.append({"session_id": row.id, "reason": f"stop failed: {exc}"})
                continue
            result = await self._cmd_session_prune({"session_id": row.id})
            if result.get("success"):
                pruned.append(row.id)
            else:
                skipped.append({"session_id": row.id,
                                "reason": result.get("error", "prune failed")})
        return {"success": True, "count": len(candidates), "pruned": pruned,
                "skipped": skipped}

    async def _cmd_session_kill(self, args: dict) -> dict:
        """Fenced kill.  The task then goes through the exit classifier.

        This deliberately does **not** transition the task, **and does not
        write ``state`` on the row either**: the next reconciler tick sees a
        dead process and classifies it.  ``desired_state="stopped"`` records
        that this exit was requested, so a pool kill does not quarantine its
        project as a rapid crash.  A human killing a session can never
        accidentally mark a task complete.

        Writing ``state="stopped"`` here used to *guarantee* the opposite of
        that docstring.  ``_step_exits`` iterates live rows only, so dropping
        the row out of ``_LIVE_STATES`` meant nothing ever classified the
        exit: the task stayed IN_PROGRESS forever, the agent BUSY, the
        workspace locked.  The row is left live and the ``process_alive``
        probe — which is now the *only* thing that decides a session is
        dead — reports what actually happened.

        Isolation is the shared session fence
        (:meth:`_session_project_scope_error`), and it has to be here:
        ``check_command_scope`` pins ``args["project_id"]`` for a per-project
        elevated token, but this command takes the row by id, unique id
        prefix, name or task and never reads that field, so the pin bought
        nothing.  ``aq session kill <8-char-prefix>`` — the prefix
        ``aq session list`` prints — is enough to stop another project's
        worker.  The fence runs on the resolved session, so every way of
        naming one is covered, and it runs before the provider is touched.
        """
        session, err = await self._resolve_session(args)
        if err:
            return err
        if (denied := self._session_project_scope_error(session)) is not None:
            return denied
        provider = self._provider_for_session(session)
        if provider is None:
            return {"error": f"Provider '{session.provider}' is not available"}
        # Intent, not observation: state stays live so the exit classifier
        # can still run.  Persist intent before signalling because a tick can
        # observe the death while stop() is waiting for the provider.
        await self.db.update_session(session.id, desired_state="stopped")
        try:
            await provider.stop(
                self._session_handle(session), grace=float(args.get("grace") or 2.0)
            )
        except Exception as exc:
            return {"error": f"kill failed: {exc}"}
        await self.orchestrator.bus.emit(
            "session.killed",
            {
                "session_id": session.id,
                "name": session.name,
                "task_id": session.task_id,
                "project_id": session.project_id,
            },
        )
        return {
            "success": True,
            "session_id": session.id,
            "state": session.state,
            "note": (
                "process signalled; the next reconciler tick classifies the exit "
                "and releases the task"
            ),
        }

    async def _cmd_session_token(self, args: dict) -> dict:
        """Mint a fresh API bearer token for an existing session.

        **Dev / e2e facility.**  Normally a session's token is minted once,
        at launch, and handed to the process through its environment
        (``src/orchestrator/execution.py`` for task sessions,
        ``PoolsMixin._launch_pool_session`` for pool workers) — nothing
        outside the session ever needs it.  The functional test kit
        (``scripts/e2e-smoke.sh``) is the exception: with
        ``sessions.provider: fake`` no real agent runs, so the harness has
        to *act as* the pool worker, and acting as it means holding its
        token.

        Deliberately **not** in :data:`~src.api.scope.AGENT_COMMAND_SET`,
        so a plain session token cannot mint another session's token: the
        command is reachable only from a trusted local caller (the CLI on
        loopback with no bearer) or an elevated supervisor token, which is
        the same trust level that can already kill the session outright.

        A **per-project** elevated token is trusted for its own project and
        no further.  ``check_command_scope`` pins ``args["project_id"]`` for
        such a caller, but this command addresses a session by id (or
        name, or task) and never reads ``project_id`` — so without the fence
        in :meth:`_session_project_scope_error`, ``supervisor-A`` could mint a
        token for a session in project B and use it to read and write B's
        work.  A minted token is a durable credential, which makes this a
        privilege escalation rather than a scoping slip.  Local callers carry
        no project pin and stay unrestricted, as does the global supervisor.

        The new token carries the session's own scope — ``session_id``,
        ``project_id``, and ``task_id`` **only for a task session**.  A
        pool worker's scope pins no task (its task changes with every
        claim), matching what ``_launch_pool_session`` mints. A live named
        supervisor retains its launch elevation and project/global scope;
        this also supports operator recovery from an expired credential.
        """
        session, err = await self._resolve_session(args)
        if err:
            return err
        if (denied := self._session_project_scope_error(session)) is not None:
            return denied
        token_store = getattr(self.orchestrator, "token_store", None)
        if token_store is None:
            return {"success": False, "error": "no token store on this daemon"}
        supervisor = session.profile_id == "supervisor" and session.lifecycle == "named"
        if supervisor and (
            session.state not in {"starting", "running"}
            or session.desired_state != "running"
            or session.ended_at is not None
            or session.task_id is not None
            or not session.instance_token
        ):
            return {"success": False, "error": "a live named supervisor session is required"}
        task_id = session.task_id if session.lifecycle == "task" else None
        token = await token_store.mint(
            session_id=session.id,
            session_instance_token=session.instance_token,
            task_id=task_id,
            project_id=session.project_id,
            elevated=supervisor,
        )
        return {
            "success": True,
            "session_id": session.id,
            "project_id": session.project_id,
            "task_id": task_id,
            "token": token,
        }

    async def _cmd_session_sleep(self, args: dict) -> dict:
        """Record that a session is not wanted running.

        This cancels the instance's cron schedules with the intent write.
        The reconciler stops named sessions on the next tick, independently
        of idle timeout. Pool sessions drain through their claim lifecycle.
