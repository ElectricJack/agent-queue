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

from src.commands.claim_commands import remove_claim_file, remove_claim_file_if_matches
from src.database.queries.task_queries import StaleClaim
from src.models import TaskCompletion, TaskStatus
from src.sessions.provider import (
    Cap,
    CapabilityUnsupported,
    NotSubmitted,
    SessionHandle,
)
from src.sessions.reconciler import DRAIN_ACK_KEY, LIVE_SESSION_STATES as _LIVE_SESSION_STATES

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
            "quarantined_at": session.quarantined_at,
            "sleep_reason": session.sleep_reason,
            "epoch": session.epoch,
        }

    # ------------------------------------------------------------------
    # operator surface
    # ------------------------------------------------------------------

    async def _cmd_session_list(self, args: dict) -> dict:
        """List sessions with lifecycle, state, task, harness, activity."""
        sessions = await self.db.list_sessions(
            state=args.get("state"),
            desired_state=args.get("desired_state"),
            lifecycle=args.get("lifecycle"),
            project_id=args.get("project_id") or self._active_project_id,
            live_only=bool(args.get("live_only")),
        )
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
        return {"success": True, "sessions": rows, "count": len(rows)}

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
        from src.sessions.transcripts import resolve_reader

        from types import SimpleNamespace
        from src.api.auth import LOCAL_SCOPE, RequestScope
        from src.api.scope import check_command_scope

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

    async def _cmd_session_kill(self, args: dict) -> dict:
        """Fenced kill.  The task then goes through the exit classifier.

        This deliberately does **not** transition the task, **and does not
        write ``state`` on the row either**: the next reconciler tick sees a
        dead process and classifies it, so a manual kill and a crash travel
        the same path — and a human killing a session can never accidentally
        mark a task complete.

        Writing ``state="stopped"`` here used to *guarantee* the opposite of
        that docstring.  ``_step_exits`` iterates live rows only, so dropping
        the row out of ``_LIVE_STATES`` meant nothing ever classified the
        exit: the task stayed IN_PROGRESS forever, the agent BUSY, the
        workspace locked.  The row is left live and the ``process_alive``
        probe — which is now the *only* thing that decides a session is
        dead — reports what actually happened.
        """
        session, err = await self._resolve_session(args)
        if err:
            return err
        provider = self._provider_for_session(session)
        if provider is None:
            return {"error": f"Provider '{session.provider}' is not available"}
        try:
            await provider.stop(
                self._session_handle(session), grace=float(args.get("grace") or 2.0)
            )
        except Exception as exc:
            return {"error": f"kill failed: {exc}"}
        # Intent, not observation.  The docstring above is about ``state``:
        # writing that would hide the exit from the classifier.  Intent is
        # the opposite case -- a human killing a session plainly does not
        # want it back, and without this the reconciler's up-convergence
        # would restart a named session the operator just killed.
        await self.db.update_session(session.id, desired_state="stopped")
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
        no further.  ``check_command_scope`` pins ``args["project_id"]``
        for such a caller, but this command addresses a session by id (or
        name, or task) and never reads ``project_id`` — so without the
        fence below, ``supervisor-A`` could mint a token for a session in
        project B and use it to read and write B's work.  A minted token is
        a durable credential, which makes this a privilege escalation
        rather than a scoping slip.  Local callers carry no project pin and
        stay unrestricted, as does the global supervisor.

        The new token carries the session's own scope — ``session_id``,
        ``project_id``, and ``task_id`` **only for a task session**.  A
        pool worker's scope pins no task (its task changes with every
        claim), matching what ``_launch_pool_session`` mints.
        """
        session, err = await self._resolve_session(args)
        if err:
            return err
        scope_project = (self._current_scope or {}).get("project_id")
        if scope_project is not None and session.project_id != scope_project:
            return {
                "success": False,
                "error": "out of scope: session belongs to another project",
            }
        token_store = getattr(self.orchestrator, "token_store", None)
        if token_store is None:
            return {"success": False, "error": "no token store on this daemon"}
        task_id = session.task_id if session.lifecycle == "task" else None
        token = await token_store.mint(
            session_id=session.id,
            task_id=task_id,
            project_id=session.project_id,
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

        Intent only — this does not signal the process.  The reconciler's
        idle-drain branch takes it down on a later tick, or ``session kill``
        does it now.  Setting intent first is what stops the up-convergence
        branch from restarting it a tick after the kill.
        """
        return await self._set_desired_state(args, "sleeping")

    async def _cmd_session_wake(self, args: dict) -> dict:
        """Mark a sleeping named session as wanted again.

        The next reconciler tick starts it (through the same lens cold-start
        path an inbound message uses).  Waking is always explicit: nothing
        infers it from activity.
        """
        session, err = await self._resolve_session(args)
        if err:
            return err
        if session.lifecycle != "named":
            return {
                "success": False,
                "error": (
                    "only named sessions are wake-on-demand; a task session is "
                    "started by the task lifecycle"
                ),
                "session_id": session.id,
            }
        # Clear the restart budget: it counts *consecutive* failed starts,
        # and an operator asking for a wake is a fresh intent, not a retry
        # of the one that failed.
        await self.db.update_session(
            session.id, desired_state="running", restarts=0, last_activity=None
        )
        return {
            "success": True,
            "session_id": session.id,
            "desired_state": "running",
            "state": session.state,
            "note": "the next reconciler tick starts it",
        }

    async def _set_desired_state(self, args: dict, desired: str) -> dict:
        session, err = await self._resolve_session(args)
        if err:
            return err
        await self.db.update_session(session.id, desired_state=desired)
        return {
            "success": True,
            "session_id": session.id,
            "desired_state": desired,
            "state": session.state,
        }

    async def _cmd_session_drain_ack(self, args: dict) -> dict:
        """Agent-facing: "I am done, you may kill me."

        The reconciler verifies the task is actually closed before killing.
        An ack with the task still open is a *premature* drain and earns one
        nudge, not a kill — otherwise an agent could end its own task by
        acking early, which is precisely the exit-as-signal failure this
        runtime exists to remove.

        A **pool** session also gets ``desired_state="stopped"`` written
        here.  ``_step_drain_ack`` never reads the provider meta key for a
        pool row (a pool worker's teardown is
        ``_terminate_pool_session``, not the task-session stop path) — it
        keys entirely off ``desired_state``.  Writing only ``state`` left an
        acked worker parked in ``draining`` forever: agent never RETIRED,
        workspace lock never released, token never revoked.  The intent is
        unambiguous — the agent just said it is finished, and for a pool
        worker there is no further work to come back for
        (``session_exhausted`` / ``drain_requested`` are the only two
        results that tell it to ack).
        """
        session, err = await self._resolve_session(args)
        if err:
            return err
        provider = self._provider_for_session(session)
        if provider is None:
            return {"error": f"Provider '{session.provider}' is not available"}
        try:
            await provider.set_meta(self._session_handle(session), DRAIN_ACK_KEY, "1")
        except Exception as exc:
            return {"error": f"could not record drain-ack: {exc}"}
        if session.lifecycle == "pool":
            await self.db.update_session(
                session.id, state="draining", desired_state="stopped"
            )
            await self.orchestrator.bus.emit(
                "pool.session_drained",
                {
                    "project_id": session.project_id,
                    "profile_id": session.profile_id,
                    "session_id": session.id,
                    "name": session.name,
                    "reason": "drain_ack",
                },
            )
        else:
            await self.db.update_session(session.id, state="draining")
        return {
            "success": True,
            "session_id": session.id,
            "state": "draining",
            "note": "acknowledged; the reconciler stops this session once the task is closed",
        }

    # ------------------------------------------------------------------
    # agent surface — the completion protocol
    # ------------------------------------------------------------------

    async def _scoped_held_task_id(self) -> str | None:
        """``sessions.task_id`` of the session in scope, if any (I3).

        A pool worker's task changes with every claim, so the completion
        protocol tells it to run ``aq task close --outcome … --claim-next``
        with no TASK_ID; the CLI then sends none and the daemon resolves it
        from whatever the calling session currently holds.
        """
        session_id = (self._current_scope or {}).get("session_id")
        if not session_id:
            return None
        session = await self.db.get_session(str(session_id))
        return session.task_id if session is not None else None

    async def _cmd_task_close(self, args: dict) -> dict:
        """Close a task with an outcome.  Backs ``aq task close``.

        Validates the caller, records outcome metadata (typed
        ``task_metadata`` keys per the work-graph contract — no new
        columns), runs the completion pipeline, and transitions the task.

        Caller validation: when the request carries a ``session_id`` (the
        CLI forwards ``AQ_SESSION_ID``) it must be the session that owns
        this task.  A session closing someone else's task is refused —
        that is either a bug or an agent that wandered, and both should be
        loud rather than silently accepted.
        """
        task_id = args.get("task_id") or await self._scoped_held_task_id()
        if not task_id:
            return {
                "success": False,
                "error": "no task_id and the session holds no task",
            }

        outcome = str(args.get("outcome") or "").strip().lower()
        if outcome not in VALID_OUTCOMES:
            return {
                "success": False,
                "error": f"outcome must be one of {list(VALID_OUTCOMES)}",
            }
        failure_class = str(args.get("failure_class") or "").strip().lower()
        if failure_class and failure_class not in VALID_FAILURE_CLASSES:
            return {
                "success": False,
                "error": f"failure_class must be one of {list(VALID_FAILURE_CLASSES)}",
            }
        work_outcome = str(args.get("work_outcome") or "").strip().lower()
        if work_outcome and work_outcome not in VALID_WORK_OUTCOMES:
            return {
                "success": False,
                "error": f"work_outcome must be one of {list(VALID_WORK_OUTCOMES)}",
            }

        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"success": False, "error": f"No task '{task_id}'"}
        if task.status not in _CLOSEABLE:
            status = getattr(task.status, "value", task.status)
            return {
                "success": False,
                "error": (
                    f"task {task_id} is {status}, not in progress — only a running "
                    "task can be closed"
                ),
            }

        caller_session_id = args.get("session_id")
        session = None
        if caller_session_id:
            session = await self.db.get_session(str(caller_session_id))
            if session is None:
                return {"success": False, "error": f"No session '{caller_session_id}'"}
            if session.task_id and session.task_id != task_id:
                return {
                    "success": False,
                    "error": (
                        f"session {session.id} owns task {session.task_id}, not "
                        f"{task_id} — refusing to close another task's work"
                    ),
                }
        if session is None:
            session = await self.db.get_session_for_task(str(task_id))

        # --- claim fence (swarm-work-model §10) ----------------------------
        # A session-scoped caller must hold the task under a current claim
        # epoch: pool sessions are required to send ``claim_epoch`` (read
        # from ``.aq/claim.json``), task sessions may omit it (legacy).
        # Local/elevated callers (no session in scope) are not fenced.
        claim_epoch = args.get("claim_epoch")
        scope_session_id = (self._current_scope or {}).get("session_id")
        fence_err = await self._assert_session_owns(
            task_id, session_id=scope_session_id, claim_epoch=claim_epoch
        )
        if fence_err:
            return fence_err
        is_pool = bool(session and session.lifecycle == "pool")
        # A close issued *by* a session whose row is still live can be handed
        # fixable git-verification issues in place instead of reopening the
        # task — see ``PipelineContext.close_session_live``.  A local /
        # elevated close (no session in scope) has no agent to hand them to,
        # so it keeps the reopen-to-READY behaviour.
        session_live = bool(
            caller_session_id
            and session is not None
            and session.state in _LIVE_SESSION_STATES
        )

        # --- close-with-summary enforcement (Dv2 Phase 2 §7) --------------
        # Tasks executed by workspace-needing profiles must carry a
        # summary at close time.  This is what feeds the reviewer, the
        # dashboard completion card, and the task-summary note in the
        # vault.  Supervisor / chat-only profiles skip the requirement
        # because they never touch a repo.  Checked *before* the
        # container-close block below: a refused close must never abandon
        # a single descendant (spec §7).
        summary = str(args.get("summary") or "").strip()
        profile = None
        if task.profile_id:
            profile = await self.db.get_profile(task.profile_id)
        needs_ws = profile.needs_workspace if profile else False
        if needs_ws and not summary:
            return {
                "success": False,
                "error": (
                    "summary is required for tasks whose profile has "
                    "needs_workspace: true (Dv2 Phase 2 §7 close contract)"
                ),
            }

        # Container-close semantics (swarm-work-model §7).
        open_children = await self.db.open_children(task_id)
        abandoned: list[str] = []
        if open_children:
            if not args.get("abandon_children"):
                return {
                    "success": False,
                    "code": "hierarchy.open_children",
                    "error": (
                        f"task {task_id} has {len(open_children)} open child(ren); close them "
                        "first or pass abandon_children=true"
                    ),
                    "open_children": open_children,
                }
            # The refusal is decided INSIDE the transaction (so no session can
            # start between the check and the abandon) but returned OUTSIDE it
            # — returning from within ``async with`` would commit the
            # transaction rather than leave it untouched.
            live: list = []
            paused: list[str] = []
            abandon_result = None
            async with self.db.immediate() as conn:
                # ``exclude_root``: the closing task's own session — the
                # worker calling ``task_close``, or the container-root
                # session driving it — is live by definition.  Counting it
                # made every ``--abandon-children`` close refuse, even with
                # nothing but a PAUSED unassigned child underneath.
                live = await self.db.live_descendant_sessions(
                    task_id, conn=conn, exclude_root=True
                )
                if not live:
                    # A hand-paused descendant cannot be transitioned
                    # (``ManualPauseActive``).  Detect it here so the caller
                    # gets a structured refusal naming the ids rather than an
                    # exception escaping from inside the transaction.
                    paused = await self.db.manually_paused_descendants(task_id, conn=conn)
                if not live and not paused:
                    abandon_result = await self.db.abandon_subtree(task_id, conn=conn)
            if live:
                held = sorted({t for _, t in live})
                return {
                    "success": False,
                    "code": "hierarchy.live_descendants",
                    "error": (
                        "descendants are held by live sessions: "
                        + ", ".join(held)
                        + "; stop them first (aq task stop <id> / aq session kill <name>)"
                    ),
                    "sessions": [{"session_id": s, "task_id": t} for s, t in live],
                    "live_descendants": held,
                }
            if paused:
                return {
                    "success": False,
                    "code": "hierarchy.manually_paused_descendants",
                    "error": (
                        "descendants are manually paused: "
                        + ", ".join(paused)
                        + "; resume them first (aq task resume --task-id <id>)"
                    ),
                    "manually_paused_descendants": paused,
                }
            # Post-commit: audit rows and settlement notification, same
            # sequencing as ``transition_task`` (never inside the write
            # transaction — a listener failure must not roll back the abandon).
            await self.db.log_blocked_flips(abandon_result.flipped)
            await self.db._notify_settled(abandon_result.settled)
            await self.db._notify_ready(abandon_result.ready)
            abandoned = abandon_result.abandoned

        # Outcome metadata is written first: it must survive even if the
        # pipeline explodes, because it is the record of what the agent
        # claimed happened.
        await self.db.set_task_meta(task_id, "outcome", outcome)
        if failure_class:
            await self.db.set_task_meta(task_id, "failure_class", failure_class)
        if work_outcome:
            await self.db.set_task_meta(task_id, "work_outcome", work_outcome)
        if args.get("commit"):
            await self.db.set_task_meta(task_id, "work_commit", str(args["commit"]))
        if args.get("branch"):
            # WG-5: work-graph §5 lists ``work_branch`` in the outcome-meta
            # contract; the older close surface only stored ``work_commit``.
            await self.db.set_task_meta(task_id, "work_branch", str(args["branch"]))
        if args.get("notes"):
            await self.db.set_task_meta(task_id, "close_notes", str(args["notes"]))
        if args.get("verification"):
            await self.db.set_task_meta(task_id, "verification", str(args["verification"]))
        if session is not None:
            await self.db.set_task_meta(task_id, "close_session_id", session.id)
        if summary:
            await self.db.set_task_meta(task_id, "summary", summary)

        # aq-surface Phase S2: revoke any session-scoped API bearer tokens
        # tied to the closed session.  Single choke point for terminal
        # state; the 60s cascade sweep is the safety net if this line ever
        # regresses.  Runs in ``finally`` so a pipeline exception (or a
        # failing subsystem inside ``complete_session_task``) does not
        # leave a live token in circulation.  We only reach this block
        # after caller-validation passed — early rejects (wrong session
        # owning another task, unknown session, ...) exit above and do
        # not revoke.
        stale = False
        retry_in_session = False
        try:
            expect_claim_epoch = int(claim_epoch) if claim_epoch is not None else None
            result = await self.orchestrator.complete_session_task(
                task,
                outcome=outcome,
                work_outcome=work_outcome,
                failure_class=failure_class,
                commit=str(args.get("commit") or ""),
                notes=str(args.get("notes") or ""),
                expect_claim_epoch=expect_claim_epoch,
                pool=is_pool,
                session_live=session_live,
            )
            retry_in_session = bool(result.get("verification_retry"))
        except StaleClaim as exc:
            # The up-front fence (``_assert_session_owns``) makes this a
            # narrow race — only a concurrent claim between that check and
            # this call reaches it. The session is still live and holds
            # nothing to clean up, so it keeps its token.
            stale = True
            return {"success": False, "result": "stale_claim", "error": str(exc)}
        finally:
            # Pool sessions keep their instance token — the workflow keeps
            # going (``claim_next``) so revoking here would kill it mid-loop.
            # A close refused for verification keeps its token too: the same
            # session is about to fix the git state and close again.
            if not is_pool and not stale and not retry_in_session:
                token_store = getattr(self.orchestrator, "token_store", None)
                if token_store is not None and session is not None:
                    try:
                        await token_store.revoke_session(session.id)
                    except Exception:
                        # Revoke is best-effort — expiry is the safety net.
                        pass

        if retry_in_session:
            # Not a close: the task is still IN_PROGRESS under this session's
            # claim, and the agent has to fix the listed git issues and call
            # ``aq task close`` again.  No completion record, no claim
            # release, no token revoke — nothing about the run ended.
            issues = result.get("issues") or []
            bullets = "\n".join(f"- {msg}" for msg in issues)
            return {
                "success": False,
                "result": "verification_failed",
                "task_id": task_id,
                "status": result.get("status"),
                "issues": issues,
                "feedback": result.get("feedback") or "",
                "error": (
                    "close refused: git verification found issues you can still "
                    f"fix from this workspace:\n{bullets}\n"
                    "The task is still yours (IN_PROGRESS, same claim). Fix these, "
                    "then run `aq task close` again."
                ),
            }

        final_task = await self.db.get_task(task_id)
        # Capture the final branch tip after verification/integration. The
        # pipeline may auto-commit dirty work, so a pre-pipeline SHA describes
        # the input state rather than the commit that actually closed the task.
        if needs_ws and not args.get("commit") and final_task and final_task.branch_name:
            checkout = await self.db.get_project_workspace_path(task.project_id)
            if checkout:
                sha = await self.orchestrator.git.arev_parse(
                    checkout, final_task.branch_name
                )
                if sha:
                    await self.db.set_task_meta(task_id, "work_commit_auto", sha)
        explicit_commit = str(args.get("commit") or "").strip()
        auto_commit = await self.db.get_task_meta(task_id, "work_commit_auto")
        commit = explicit_commit or str(auto_commit or "").strip()

        def _string_list(value) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value] if value.strip() else []
            return [str(item) for item in value if str(item).strip()]

        # Append only: a reopened task may be closed again, and both accounts
        # remain available while task detail shows the latest one.
        await self.db.save_task_completion(
            TaskCompletion(
                id=str(uuid.uuid4()),
                task_id=task_id,
                outcome=outcome,
                work_outcome=work_outcome or None,
                failure_class=failure_class or None,
                changes=str(args.get("changes") or summary).strip(),
                verification=str(args.get("verification") or "").strip(),
                tests=_string_list(args.get("tests")),
                commands=_string_list(args.get("commands")),
                branch=(final_task.branch_name if final_task else task.branch_name),
                commits=[commit] if commit else [],
                pr_url=(
                    (final_task.pr_url if final_task else None)
                    or result.get("pr_url")
                    or task.pr_url
                ),
                summary=summary,
                notes=str(args.get("notes") or "").strip(),
                completed_at=time.time(),
            )
        )

        await self.db.record_task_session_outcome(
            task_id, outcome, session_id=session.id if session is not None else None,
        )

        if is_pool:
            # The workspace agent-lock is retained (``terminate_pool_session``
            # is the only path that drops it); only the task-hold is released.
            await self.db.release_claim(
                session.id,
                task_status=TaskStatus(result["status"]),
                context="session_close",
                now=time.time(),
                expected_task_id=task_id,
                expected_claim_epoch=expect_claim_epoch,
                drain_after_release=self.config.swarm.fresh_context_per_task,
            )
            remove_claim_file_if_matches(session.work_dir, task_id, expect_claim_epoch)
        elif session is not None and session.lifecycle == "task" and session.work_dir:
            # Push launches join the claim fence too (execution.py) and
            # write the same claim file — clean it up on a task-session
            # close just like the pool-release path does.
            remove_claim_file(session.work_dir)

        response = {
            "success": True,
            "task_id": task_id,
            "outcome": outcome,
            "work_outcome": work_outcome or None,
            "abandoned": abandoned,
            "next_step": "run `aq session drain-ack` to release this session",
            **result,
        }
        if args.get("claim_next"):
            response["next"] = await self._cmd_task_claim(
                {"next": True, "wait": int(args.get("wait") or 0)}
            )
        return response

    async def _cmd_task_heartbeat(self, args: dict) -> dict:
        """Refresh this task's agent lease.  Backs ``aq task heartbeat``.

        Two cheap writes: ``agents.last_heartbeat`` (what the rest of the
        daemon already reads) and ``sessions.last_activity`` (what the
        stall ladder measures).  Returns the new lease expiry so an agent
        about to run something long can see how much runway it has.
        """
        task_id = args.get("task_id")
        session = None
        if not task_id and args.get("session_id"):
            session = await self.db.get_session(str(args["session_id"]))
            if session is not None:
                task_id = session.task_id
        if not task_id:
            task_id = await self._scoped_held_task_id()
        if not task_id:
            return {"success": False, "error": "no task_id and the session holds no task"}

        err = await self._assert_session_owns(
            task_id,
            session_id=(self._current_scope or {}).get("session_id"),
            claim_epoch=args.get("claim_epoch"),
        )
        if err:
            return err

        task = await self.db.get_task(str(task_id))
        if task is None:
            return {"error": f"No task '{task_id}'"}

        now = time.time()
        if session is None:
            session = await self.db.get_session_for_task(str(task_id))
        if session is not None:
            await self.db.touch_session_activity(session.id, now)
        if task.assigned_agent_id:
            try:
                await self.db.update_agent(task.assigned_agent_id, last_heartbeat=now)
            except Exception:
                logger.debug("heartbeat: agent update failed", exc_info=True)

        ttl = self.config.sessions.lease_ttl_seconds
        return {
            "success": True,
            "task_id": task_id,
            "session_id": session.id if session else None,
            "heartbeat_at": now,
            "lease_expires_at": now + ttl if ttl > 0 else None,
        }
