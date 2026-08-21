"""SessionLens — the narrow window from delivery engine into session runtime.

The :class:`MessageDeliveryEngine` (Task 2) needs four things and four only:
"is this recipient reachable and busy right now?", "spin one up if it is
the supervisor and it is asleep", "type something at it", and "what did it
say last?". The rest of the session runtime — reconciliation, adoption,
stall ladders, transcripts fan-out — is none of the engine's business.

That is what :class:`SessionManagerProto` codifies, and what
:class:`SessionLens` implements against the real DB + provider registry +
spec builder. The lens is read-only over the ``sessions`` table (owned by
:class:`~src.sessions.reconciler.SessionReconciler`) and only ever *starts*
one kind of session: the supervisor, on demand, because the supervisor is
wake-able by design. Task and agent sessions are launched by the
orchestrator's task lifecycle; the messenger has no license to spawn one.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Literal, Protocol, runtime_checkable

from src.sessions.provider import (
    CapabilityUnsupported,
    NotSubmitted,
    SessionHandle,
)
from src.sessions.spec import named_session_name, task_session_name

logger = logging.getLogger(__name__)

__all__ = ["Activity", "SessionManagerProto", "SessionLens"]


#: Coarse activity signal reported to the delivery engine.
#:
#: * ``idle``     — live and quiet; safe to nudge.
#: * ``busy``     — live and mid-turn; delivery engine skips this pass and
#:                  lets the ``UserPromptSubmit`` inject hook catch the
#:                  message at the next prompt boundary.
#: * ``sleeping`` — a wake-able target (supervisor) with no live session.
#: * ``absent``   — no live session and the messenger must not spawn one
#:                  (task/agent sessions are launched by the task lifecycle).
Activity = Literal["idle", "busy", "sleeping", "absent"]


#: Seconds since the provider last observed output within which a session
#: is considered *busy*. Matches the design spec (§6.1): ~one prompt turn
#: including a small tool call. The delivery engine deliberately errs on
#: the side of "wait one cycle" over "interrupt mid-turn".
_BUSY_WINDOW_SECONDS: float = 30.0


@runtime_checkable
class SessionManagerProto(Protocol):
    """Narrow window from delivery engine into the session runtime.

    Names must not drift — Task 2's ``MessageDeliveryEngine`` binds to
    these signatures verbatim.
    """

    async def activity(
        self, *, kind: str, target_id: str, project_id: str | None
    ) -> Activity:
        """Coarse activity signal for a message recipient."""
        ...

    async def ensure_started(
        self, *, kind: str, target_id: str, project_id: str | None
    ) -> bool:
        """Wake the supervisor on demand. No-op (False) for other kinds."""
        ...

    async def nudge(
        self,
        *,
        kind: str,
        target_id: str,
        project_id: str | None,
        text: str,
    ) -> bool:
        """Inject *text* at the target's live session; False if not delivered."""
        ...

    async def tail_assistant_turn(
        self,
        *,
        kind: str,
        target_id: str,
        project_id: str | None,
        since: float,
    ) -> str | None:
        """Last assistant turn newer than *since*, or ``None``."""
        ...


class SessionLens:
    """Production :class:`SessionManagerProto` over the session runtime.

    Constructed once by the orchestrator (Task 3 wires it into
    :mod:`src.orchestrator.core`); the delivery engine holds a reference.

    Read-only over the ``sessions`` table: rows are owned by the
    reconciler. The one write path is :meth:`ensure_started` for the
    supervisor, which delegates to the provider registry — the same code
    path the reconciler uses.
    """

    def __init__(
        self,
        *,
        db,
        providers,
        spec_builder,
        harness_registry,
        config,
        profiles_loader,
    ):
        self._db = db
        self._providers = providers
        self._spec_builder = spec_builder
        self._harnesses = harness_registry
        self._config = config
        #: Async callable ``profile_id -> AgentProfile | None``. The
        #: supervisor profile ships in Task 6; tests inject a fake here.
        self._profiles_loader = profiles_loader

    # -- SessionManagerProto ------------------------------------------------

    async def activity(
        self, *, kind: str, target_id: str, project_id: str | None
    ) -> Activity:
        row, handle = await self._resolve(kind=kind, target_id=target_id, project_id=project_id)
        if row is None or handle is None:
            return "sleeping" if kind == "supervisor" else "absent"

        provider = self._providers.create(row.provider)
        try:
            running = await provider.is_running(handle)
        except Exception:  # provider misbehavior is not the engine's problem
            logger.debug("is_running failed for %s", row.name, exc_info=True)
            running = False
        if not running:
            return "sleeping" if kind == "supervisor" else "absent"

        try:
            last = await provider.last_activity(handle)
        except Exception:
            logger.debug("last_activity failed for %s", row.name, exc_info=True)
            last = None
        if last is not None and (time.time() - last) <= _BUSY_WINDOW_SECONDS:
            return "busy"
        return "idle"

    async def ensure_started(
        self, *, kind: str, target_id: str, project_id: str | None
    ) -> bool:
        # Only the supervisor is wake-on-demand. Task and agent sessions
        # are launched by the task lifecycle; spawning one from the
        # message path would race the orchestrator and violate ownership.
        if kind != "supervisor":
            return False

        row, handle = await self._resolve(kind=kind, target_id=target_id, project_id=project_id)
        if row is not None and handle is not None:
            provider = self._providers.create(row.provider)
            try:
                if await provider.is_running(handle):
                    return True
            except Exception:
                logger.debug("is_running failed for %s", row.name, exc_info=True)

        # Cold start.
        profile = await self._profiles_loader("supervisor")
        if profile is None:
            logger.warning("supervisor profile not found; cannot start session")
            return False

        harness_name = getattr(profile, "harness", None) or "claude"
        harness = self._harnesses.get(harness_name, project_id=project_id)
        if harness is None:
            logger.warning("harness %r not registered; cannot start supervisor", harness_name)
            return False

        # Match the reconciler exactly: use the operator's configured
        # provider. Tests must register the fake under that same name (or
        # set ``config.sessions.provider = "fake"``) rather than relying on
        # a production special-case that would mask a misconfigured host.
        sessions_cfg = getattr(self._config, "sessions", None)
        provider_name = getattr(sessions_cfg, "provider", None) or "subprocess"
        try:
            provider = self._providers.create(provider_name)
        except ValueError:
            logger.warning(
                "configured session provider %r not registered; cannot start supervisor",
                provider_name,
            )
            return False

        work_dir = self._supervisor_work_dir(project_id)
        if not work_dir:
            # No vault_root configured and no project_id → nowhere sensible
            # to run the supervisor. Better to skip than to pass an empty
            # cwd to the harness and get a confusing downstream failure.
            logger.debug(
                "supervisor work_dir is empty (vault_root unset, project_id=%r); "
                "refusing to start",
                project_id,
            )
            return False
        # `claude --session-id` rejects bare hex — must be dashed.
        session_id = str(uuid.uuid4())
        instance_token = uuid.uuid4().hex[:12]

        spec = self._spec_builder.build_named_spec(
            profile=profile,
            harness=harness,
            project_id=project_id,
            work_dir=work_dir,
            session_id=session_id,
            instance_token=instance_token,
            # Epoch is provenance only; the reconciler will overwrite when
            # it adopts. Passing empty is safe (see spec builder).
            epoch="",
        )
        try:
            await provider.start(spec)
        except Exception:
            logger.exception("failed to start supervisor session for project=%s", project_id)
            return False
        return True

    async def nudge(
        self,
        *,
        kind: str,
        target_id: str,
        project_id: str | None,
        text: str,
    ) -> bool:
        row, handle = await self._resolve(kind=kind, target_id=target_id, project_id=project_id)
        if row is None or handle is None:
            return False
        provider = self._providers.create(row.provider)
        try:
            await provider.nudge(handle, text)
        except (NotSubmitted, CapabilityUnsupported):
            # Row stays pending. The delivery engine retries with backoff.
            return False
        except Exception:
            logger.exception("nudge failed for %s", row.name)
            return False
        return True

    async def tail_assistant_turn(
        self,
        *,
        kind: str,
        target_id: str,
        project_id: str | None,
        since: float,
    ) -> str | None:
        """Last assistant turn on the target's transcript newer than *since*.

        The watcher (:class:`~src.sessions.transcripts.watcher.TranscriptWatcher`)
        is streaming-only — it holds byte offsets, not entries — so this
        opens the reader itself and reads the whole file. That is fine at
        this cadence: the delivery engine only reaches for the fallback
        after ``reply_timeout`` (default 120 s), one recipient at a time.
        """
        from src.sessions.transcripts import resolve_reader

        row, _handle = await self._resolve(
            kind=kind, target_id=target_id, project_id=project_id
        )
        if row is None:
            return None
        reader = resolve_reader(row.harness)
        if reader is None:
            return None
        path = reader.resolve_path(row.work_dir, row.session_key)
        if path is None:
            return None
        try:
            entries, _ = await reader.read_new(path, 0)
        except Exception:
            logger.debug("tail read failed for %s", row.name, exc_info=True)
            return None
        for entry in reversed(entries):
            if entry.type == "assistant" and entry.ts and entry.ts > since:
                return entry.text or None
        return None

    # -- internals ----------------------------------------------------------

    async def _resolve(
        self, *, kind: str, target_id: str, project_id: str | None
    ) -> tuple[object | None, SessionHandle | None]:
        """Map (kind, target_id, project_id) → (session row, handle).

        Read-only. The reconciler owns writes to ``sessions``.
        """
        row = None
        if kind == "task":
            row = await self._db.get_session_by_name(task_session_name(target_id))
        elif kind == "agent":
            # Message `to_kind='agent'` carries an agent id. Sessions
            # aren't indexed by agent, but the agents table tracks
            # `current_task_id`; resolve via that. Unknown agent or one
            # with no current task → no session (caller reports "absent").
            agent = await self._db.get_agent(target_id)
            if agent is None or not agent.current_task_id:
                return None, None
            row = await self._db.get_session_for_task(agent.current_task_id)
        elif kind == "supervisor":
            row = await self._db.get_session_by_name(
                named_session_name("supervisor", project_id)
            )
        else:
            return None, None

        if row is None:
            return None, None
        handle = SessionHandle(
            name=row.name,
            provider=row.provider,
            instance_token=row.instance_token,
        )
        return row, handle

    def _supervisor_work_dir(self, project_id: str | None) -> str:
        """Where the supervisor session runs.

        Spec §6: the project's vault when there is one, otherwise the
        system vault root. Vault-only; a supervisor is a chat brain, not
        a code worker, and does not need a workspace attachment.
        """
        vault_root = getattr(self._config, "vault_root", "") or ""
        if project_id and vault_root:
            return os.path.join(vault_root, "projects", project_id)
        return vault_root
