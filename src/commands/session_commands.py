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

import logging
import time

from src.models import TaskStatus
from src.sessions.provider import (
    Cap,
    CapabilityUnsupported,
    NotSubmitted,
    SessionHandle,
)
from src.sessions.reconciler import DRAIN_ACK_KEY

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

        session, err = await self._resolve_session(args)
        if err:
            return err

        # Default cap of 100 entries per call: rereading a large JSONL and
        # returning every entry over the CLI/MCP boundary produces multi-MB
        # payloads for long sessions.  Callers who want a bigger window
        # pass ``limit``/``lines``/``n`` explicitly.
        base_dir = getattr(self.orchestrator, "transcript_base_dir", None)
        reader = resolve_reader(session.harness, base_dir=base_dir)
        if reader is not None:
            path = reader.resolve_path(session.work_dir, session.session_key)
            if path is not None:
                try:
                    # ``read_new`` runs the blocking file IO in
                    # ``asyncio.to_thread`` so a big transcript on a slow
                    # disk does not stall the event loop even though we
                    # read the whole file here before tailing.
                    entries, _ = await reader.read_new(path, 0)
                except Exception:
                    entries = []
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
        task_id = args.get("task_id")
        if not task_id:
            return {"success": False, "error": "task_id is required"}

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
            immediate = getattr(self.db, "immediate", None) or self.db._engine.begin
            async with immediate() as conn:
                live = await self.db.live_descendant_sessions(task_id, conn=conn)
                if live:
                    return {
                        "success": False,
                        "code": "hierarchy.live_descendants",
                        "error": "descendants are held by live sessions; stop them first "
                        "(aq task stop <id> / aq session kill <name>)",
                        "sessions": [{"session_id": s, "task_id": t} for s, t in live],
                    }
                abandon_result = await self.db.abandon_subtree(task_id, conn=conn)
            # Post-commit: audit rows and settlement notification, same
            # sequencing as ``transition_task`` (never inside the write
            # transaction — a listener failure must not roll back the abandon).
            await self.db.log_blocked_flips(abandon_result.flipped)
            await self.db._notify_settled(abandon_result.settled)
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

        # Capture commit hash from branch when the agent did not supply
        # ``commit`` explicitly.  Best-effort — a missing branch, a
        # missing checkout, or a git error all leave ``work_commit_auto``
        # unset (rather than failing the close).
        if needs_ws and not args.get("commit") and task.branch_name:
            checkout = await self.db.get_project_workspace_path(task.project_id)
            if checkout:
                sha = await self.orchestrator.git.arev_parse(checkout, task.branch_name)
                if sha:
                    await self.db.set_task_meta(task_id, "work_commit_auto", sha)

        # aq-surface Phase S2: revoke any session-scoped API bearer tokens
        # tied to the closed session.  Single choke point for terminal
        # state; the 60s cascade sweep is the safety net if this line ever
        # regresses.  Runs in ``finally`` so a pipeline exception (or a
        # failing subsystem inside ``complete_session_task``) does not
        # leave a live token in circulation.  We only reach this block
        # after caller-validation passed — early rejects (wrong session
        # owning another task, unknown session, ...) exit above and do
        # not revoke.
        try:
            result = await self.orchestrator.complete_session_task(
                task,
                outcome=outcome,
                work_outcome=work_outcome,
                failure_class=failure_class,
                commit=str(args.get("commit") or ""),
                notes=str(args.get("notes") or ""),
            )
        finally:
            token_store = getattr(self.orchestrator, "token_store", None)
            if token_store is not None and session is not None:
                try:
                    await token_store.revoke_session(session.id)
                except Exception:
                    # Revoke is best-effort — expiry is the safety net.
                    pass
        return {
            "success": True,
            "task_id": task_id,
            "outcome": outcome,
            "work_outcome": work_outcome or None,
            "abandoned": abandoned,
            "next_step": "run `aq session drain-ack` to release this session",
            **result,
        }

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
            return {"error": "task_id is required (or a session_id that owns one)"}

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
