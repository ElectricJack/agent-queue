"""Explicit, task-free interactive terminals for durable agent definitions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from src.agents.configuration import (
    SUPERVISOR_AGENT_ID,
    apply_agent_overrides,
    resolve_launch_settings,
)
from src.models import AgentState, SessionRecord, TaskStatus
from src.sessions.provider import Cap, SessionExecutableNotFound, SessionHandle

logger = logging.getLogger(__name__)
_ACTIVE_TASK = {
    TaskStatus.ASSIGNED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.WAITING_INPUT,
}


class TerminalStartError(ValueError):
    """A safe, user-facing reason an explicit terminal could not start."""


def terminal_name(agent_id: str) -> str:
    # A stable opaque component prevents path traversal and sanitization collisions.
    return "n-agent-" + hashlib.sha256(agent_id.encode()).hexdigest()


def _interactive_provider(orchestrator, name):
    try:
        provider = orchestrator.session_providers.create(name)
    except ValueError as exc:
        raise TerminalStartError("Configured session provider is not available") from exc
    if Cap.INPUT not in provider.capabilities:
        raise TerminalStartError(
            "This session provider does not support interactive terminal input"
        )
    return provider


async def _existing_terminal(orchestrator, agent):
    sessions = await orchestrator.db.list_sessions(agent_id=agent.id, live_only=True)
    if agent.current_task_id:
        for row in await orchestrator.db.list_sessions(live_only=True):
            if (
                row.task_id == agent.current_task_id
                and row.agent_id is None
                and all(s.id != row.id for s in sessions)
            ):
                sessions.append(row)
    if len(sessions) > 1:
        raise TerminalStartError("Agent has multiple live sessions; reconcile them before starting")
    if not sessions:
        return None
    row = sessions[0]
    if row.task_id:
        task = await orchestrator.db.get_task(row.task_id)
        if (
            task is None
            or task.assigned_agent_id != agent.id
            or agent.current_task_id != task.id
            or task.status not in _ACTIVE_TASK
            or (row.last_claim_epoch is not None and row.last_claim_epoch != task.claim_epoch)
        ):
            raise TerminalStartError("Agent has a live session with a different task assignment")
    elif agent.current_task_id:
        raise TerminalStartError("Agent has a live terminal and a different task assignment")
    provider = _interactive_provider(orchestrator, row.provider)
    try:
        running = await provider.is_running(
            SessionHandle(row.name, row.provider, row.instance_token)
        )
    except Exception as exc:
        raise TerminalStartError("Could not confirm the agent's live terminal") from exc
    if not running:
        raise TerminalStartError("Agent session is awaiting reconciliation; retry after it stops")
    return row


async def start_agent_terminal(orchestrator, agent_id: str, *, config=None) -> SessionRecord:
    """Start/resume on request, or return this agent's existing live session.

    The database reservation also fences push/pool assignment. The local lock
    makes repeated button presses return the same result instead of racing.
    """
    locks = getattr(orchestrator, "_agent_terminal_locks", None)
    if locks is None:
        locks = orchestrator._agent_terminal_locks = {}
    lock = locks.setdefault(agent_id, asyncio.Lock())
    async with lock:
        try:
            return await _start_locked(orchestrator, agent_id, config or orchestrator.config)
        except SessionExecutableNotFound as exc:
            raise TerminalStartError(str(exc)) from exc


async def _start_locked(orchestrator, agent_id, config):
    db = orchestrator.db
    agent = await db.get_agent(agent_id)
    if agent is None or agent.deleted_at is not None:
        raise TerminalStartError("Agent not found")
    if not agent.enabled:
        raise TerminalStartError("Agent is disabled; enable it before starting a terminal")
    existing = await _existing_terminal(orchestrator, agent)
    if existing is not None:
        return existing
    if not getattr(config.sessions, "enabled", False):
        raise TerminalStartError("Session runtime is disabled (sessions.enabled=false)")
    if getattr(orchestrator, "_paused", False):
        raise TerminalStartError("Orchestrator is paused; resume it before starting a terminal")
    if agent.current_task_id or agent.state != AgentState.IDLE:
        raise TerminalStartError("Agent is busy; wait for its current work to finish")
    token_store = getattr(orchestrator, "token_store", None)
    if token_store is None:
        raise TerminalStartError("Session token service is unavailable; terminal was not started")
    provider_name = getattr(config.sessions, "provider", None) or "subprocess"
    provider = _interactive_provider(orchestrator, provider_name)

    if agent.id == SUPERVISOR_AGENT_ID and agent.role == "supervisor":
        lens = orchestrator.session_lens
        # Explicit starts must never fall through the legacy no-token test path.
        if lens._token_store is None:
            raise TerminalStartError("Supervisor token service is unavailable")
        if not await lens.ensure_started(
            kind="session",
            target_id=agent.id,
            project_id=None,
            raise_start_error=True,
        ):
            raise TerminalStartError(
                "Supervisor could not start; check its profile and session logs"
            )
        row = await _existing_terminal(orchestrator, await db.get_agent(agent.id))
        if row is None:
            raise TerminalStartError("Supervisor did not publish a live session")
        return row
    if agent.role != "worker":
        raise TerminalStartError("Only workers and the canonical supervisor have terminals")
    profile = await db.get_profile(agent.profile_id) if agent.profile_id else None
    if profile is None:
        raise TerminalStartError("Agent needs a valid profile before starting")
    profile = apply_agent_overrides(profile, agent)
    harness = orchestrator.harness_registry.get(profile.harness or "claude", project_id=None)
    if harness is None:
        raise TerminalStartError("Agent harness is not registered")
    if not await db.reserve_idle_agent(agent.id):
        raise TerminalStartError("Agent is busy or already starting a session")
    reservation = await db.get_agent(agent.id)
    session_id = str(uuid4())
    instance_token = uuid4().hex[:12]
    name = terminal_name(agent.id)
    handle = SessionHandle(name, provider_name, instance_token)
    launch_attempted = False
    try:
        data_root = Path(config.data_dir).expanduser().resolve()
        root = data_root / "agent-terminals"
        if root.is_symlink() or not root.resolve().is_relative_to(data_root):
            raise TerminalStartError("Agent terminal root must stay inside the data directory")
        work_dir = root / name.removeprefix("n-agent-")
        work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if work_dir.is_symlink() or work_dir.resolve().parent != root.resolve():
            raise TerminalStartError("Agent terminal workspace must be a private directory")
        history = await db.list_sessions(agent_id=agent.id, lifecycle="named")
        previous = max(
            (
                s
                for s in history
                if s.name == name
                and s.project_id is None
                and s.task_id is None
                and s.harness == harness.id
                and s.profile_id == profile.id
                and s.work_dir == str(work_dir)
                and s.state in {"stopped", "sleeping"}
            ),
            key=lambda s: s.started_at or 0,
            default=None,
        )
        resume_key = (
            previous.session_key
            if previous is not None
            and profile.wake_mode != "fresh"
            and harness.resume.style != "none"
            else None
        )
        api_token = await token_store.mint(
            session_id=session_id,
            task_id=None,
            project_id=None,
            elevated=False,
        )
        if not api_token:
            raise TerminalStartError("Session token service returned no token")
        builder = orchestrator.session_spec_builder
        spec = builder.build_named_spec(
            profile=profile,
            harness=harness,
            project_id=None,
            work_dir=str(work_dir),
            session_id=session_id,
            instance_token=instance_token,
            epoch=getattr(orchestrator, "daemon_epoch", ""),
            api_token=api_token,
            wake="resume" if resume_key else "fresh",
            resume_key=resume_key,
            prompt=(
                "You are an interactive agent terminal. Wait for the user's instructions here. "
                "You have no assigned project or task. Do not claim tasks, create tasks, "
                "or poll the message inbox automatically."
            ),
        )
        env = dict(spec.env)
        env.update(AQ_SESSION_NAME=name, AQ_AGENT_ID=agent.id, AQ_SESSION_KIND="named")
        # A harness/daemon environment must not lend an unrelated task identity.
        for key in ("AQ_TASK_ID", "AQ_CLAIM_EPOCH"):
            env.pop(key, None)
        spec = replace(spec, session_name=name, env=env)
        current = await db.get_agent(agent.id)
        if (
            not current
            or not current.enabled
            or current.deleted_at is not None
            or current.current_task_id is not None
            or current.state != AgentState.BUSY
            or current.last_heartbeat != reservation.last_heartbeat
        ):
            raise TerminalStartError("Agent settings or ownership changed during startup")
        launch_attempted = True
        launched_at = time.time()
        await provider.start(spec)
        row = SessionRecord(
            id=session_id,
            agent_id=agent.id,
            project_id=None,
            task_id=None,
            profile_id=profile.id,
            harness=harness.id,
            provider=provider_name,
            name=name,
            lifecycle="named",
            work_dir=str(work_dir),
            epoch=getattr(orchestrator, "daemon_epoch", ""),
            instance_token=instance_token,
            started_at=launched_at,
            session_key=resume_key or (session_id if harness.session_id_flag else None),
            state="running",
            desired_state="running",
            hooks_provisioned=spec.hooks_provisioned,
            **resolve_launch_settings(profile, harness, builder),
        )
        await db.create_session(row, release_agent_reservation=True)
        return row
    except BaseException as exc:
        stopped = not launch_attempted
        if launch_attempted:
            try:
                await provider.stop(handle)
                stopped = not await provider.is_running(handle)
            except Exception:
                logger.exception("Could not confirm terminal cleanup for agent %s", agent.id)
        try:
            await token_store.revoke_session(session_id)
        except Exception:
            logger.exception("Could not revoke failed terminal token for session %s", session_id)
        if stopped:
            await db.release_agent_reservation(
                agent.id,
                expected_heartbeat=reservation.last_heartbeat,
            )
        if isinstance(exc, (TerminalStartError, SessionExecutableNotFound, asyncio.CancelledError)):
            raise
        logger.exception("Could not start terminal for agent %s", agent.id)
        raise TerminalStartError(
            "Terminal could not start; check its harness and session logs"
        ) from exc
