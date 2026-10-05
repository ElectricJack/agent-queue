"""The only production entry point for launching an agent process.

The sessions table is the flock's execution registry. Its STARTING row must
commit before a provider is allowed to spawn, including named supervisors.
"""
from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import replace

from src.models import SessionRecord
from src.sessions.provider import SessionDiedDuringStartup, SessionHandle, SessionSpec

logger = logging.getLogger(__name__)
_launching: set[tuple[int, str]] = set()
_active_authorizations: set[tuple[int, int]] = set()
_authorized: ContextVar[tuple[int, SessionSpec] | None] = ContextVar("flock_launch", default=None)


def require_launch_authorization(provider, spec: SessionSpec) -> None:
    """Production providers reject starts outside the registered launch scope."""
    authorization = _authorized.get()
    if (authorization is None or authorization[0] != id(provider) or authorization[1] is not spec
            or (id(provider), id(spec)) not in _active_authorizations):
        raise PermissionError("agent launch requires a committed flock registration")


def launch_in_progress(db, session_id: str) -> bool:
    """A reconciler must not reap a committed row while its launcher awaits."""
    return (id(db), session_id) in _launching


async def _emit(bus, event: str, record: SessionRecord) -> None:
    if bus is not None:
        try:
            await bus.emit(event, {
                "session_id": record.id, "name": record.name,
                "task_id": record.task_id, "project_id": record.project_id,
                "provider": record.provider, "harness": record.harness,
                "work_dir": record.work_dir,
            })
        except Exception:
            logger.exception("Could not broadcast flock registration %s", record.id)


class SessionLaunchUncertain(RuntimeError):
    """A partial launch still owns resources and needs reconciliation."""


async def launch_session(
    db, provider, spec: SessionSpec, record: SessionRecord, *,
    registered: bool = False, release_agent_reservation: bool = False, conn=None, bus=None,
) -> SessionHandle:
    """Register, launch, then publish RUNNING; registration failures fail closed.

    Hierarchical writers register inside their branch-attachment transaction
    and pass ``registered=True`` while holding their ownership exclusion.
    That path verifies the committed registration rather than inserting twice.
    The caller owns token/workspace rollback and provider failure attribution.
    """
    key = (id(db), record.id)
    if key in _launching:
        raise ValueError("session launch is already in progress")
    _launching.add(key)
    try:
        return await _launch(
            db, provider, spec, record, registered=registered,
            release_agent_reservation=release_agent_reservation, conn=conn, bus=bus,
        )
    finally:
        _launching.discard(key)


async def _launch(db, provider, spec, record, *, registered, release_agent_reservation, conn, bus):
    if (
        spec.env.get("AQ_SESSION_ID") != record.id
        or spec.session_name != record.name
        or spec.instance_token != record.instance_token
        or provider.name != record.provider
    ):
        raise ValueError("launch specification does not match its flock registration")
    if registered:
        existing = await db.get_session(record.id)
        if (
            existing is None or existing.state != "starting"
            or existing.instance_token != record.instance_token
            or existing.name != record.name
        ):
            raise ValueError("launch has no matching STARTING flock registration")
    else:
        await db.create_session(
            replace(record, state="starting", desired_state="running"),
        )
    await _emit(bus, "session.registered", record)
    try:
        authorization = _authorized.set((id(provider), spec))
        authorization_key = (id(provider), id(spec))
        _active_authorizations.add(authorization_key)
        try:
            handle = await provider.start(spec)
        finally:
            _active_authorizations.discard(authorization_key)
            _authorized.reset(authorization)
        if handle != SessionHandle(record.name, record.provider, record.instance_token):
            raise ValueError("provider returned a different launch identity")
        await db.publish_session_running(
            record.id, record.instance_token, conn=conn,
            release_agent_reservation=release_agent_reservation,
        )
        if conn is None:
            await _emit(bus, "session.started", record)
        return handle
    except BaseException as exc:
        # A partial start must remain discoverable if stopping fails. Never
        # free its workspace by pretending a live process is stopped.
        handle = SessionHandle(record.name, record.provider, record.instance_token)
        try:
            await provider.stop(handle)
            if not await provider.confirm_stopped(handle):
                raise RuntimeError("provider still reports the launched instance")
        except BaseException as cleanup:
            raise SessionLaunchUncertain(
                f"session {record.id}: launch cleanup could not confirm stop"
            ) from cleanup
        current = await db.get_session(record.id)
        if current and current.lifecycle == "pool" and (current.task_id or current.claim_phase):
            await db.update_session(record.id, conn=conn, desired_state="stopped")
            raise SessionLaunchUncertain(
                f"session {record.id}: launch failed after a claim; reconciliation owns cleanup"
            ) from exc
        await db.update_session(
            record.id, conn=conn, state="stopped", desired_state="stopped",
            ended_at=time.time(),
            end_reason="startup_exit" if isinstance(exc, SessionDiedDuringStartup) else "launch_failed",
        )
        if conn is None:
            await _emit(bus, "session.launch_failed", record)
        raise
