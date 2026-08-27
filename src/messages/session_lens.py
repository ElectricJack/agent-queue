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
wake-able by design. Task sessions are launched by the orchestrator's task
lifecycle; the messenger has no license to spawn one.

Recipient vocabulary (aligned with the spec's ``messages.to_kind`` set —
``{session, task, profile, user}``): the lens speaks two kinds internally,
``"task"`` (a task id) and ``"session"`` (a bare session **name**, e.g.
``supervisor-<project_id>`` or ``s-<task-name>``). ``profile`` and ``user``
recipients never touch a live session so the delivery engine handles them
directly and never calls the lens for them.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Literal, Protocol, runtime_checkable

from sqlalchemy.exc import IntegrityError

from src.models import SessionRecord
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
#:                  (task sessions are launched by the task lifecycle).
Activity = Literal["idle", "busy", "sleeping", "absent"]


#: Seconds since the provider last observed output within which a session
#: is considered *busy*. Matches the design spec (§6.1): ~one prompt turn
#: including a small tool call. The delivery engine deliberately errs on
#: the side of "wait one cycle" over "interrupt mid-turn".
_BUSY_WINDOW_SECONDS: float = 30.0


#: Prefix that identifies a supervisor messaging *address* by convention;
#: the only session name the lens is licensed to cold-start (spec §6).
#: This is the address used in ``messages.to_id`` — NOT the runtime
#: session name. The runtime session is named
#: ``named_session_name("supervisor", project_id)`` → ``n-supervisor--<pid>``
#: so that :class:`~src.sessions.reconciler.SessionReconciler` can adopt it
#: (adoption only scans ``s-`` and ``n-`` prefixes).
_SUPERVISOR_NAME_PREFIX: str = "supervisor-"

#: Profile id for the supervisor. Used to derive the runtime session name
#: from a ``supervisor-<pid>`` messaging address.
_SUPERVISOR_PROFILE_ID: str = "supervisor"

#: The suffix used by the *global* supervisor's messaging address
#: (``supervisor-global``) and, correspondingly, the second half of its
#: runtime session name (``n-supervisor--global``). The global supervisor
#: is a first-class, always-available operator that talks to Agent Q at
#: ``/`` in the dashboard — distinct from any per-project supervisor. It
#: runs with an admin scope (elevated + project_id=None), lives at the
#: system vault root, and its bearer token is loopback-restricted (see
#: :class:`~src.api.middleware.TokenAuthMiddleware`).
_GLOBAL_SUPERVISOR_SUFFIX: str = "global"


def _resolve_runtime_session_name(kind: str, target_id: str) -> str:
    """Translate a messaging ``target_id`` to the runtime session name.

    The messaging layer addresses the supervisor as ``supervisor-<pid>``
    (spec §5), but the actual runtime session is named
    ``n-supervisor--<pid>`` so the reconciler can adopt it after a daemon
    restart. Every runtime-facing lookup (DB row, provider start, nudge,
    activity, transcript tail) must speak the runtime name.

    Task session names (``s-...``) and any non-supervisor session address
    pass through unchanged — those are already real runtime names.
    """
    if kind == "session" and target_id.startswith(_SUPERVISOR_NAME_PREFIX):
        project_id = target_id[len(_SUPERVISOR_NAME_PREFIX) :]
        return named_session_name(_SUPERVISOR_PROFILE_ID, project_id or None)
    return target_id


@runtime_checkable
class SessionManagerProto(Protocol):
    """Narrow window from delivery engine into the session runtime.

    Names must not drift — Task 2's ``MessageDeliveryEngine`` binds to
    these signatures verbatim.
    """

    async def activity(self, *, kind: str, target_id: str, project_id: str | None) -> Activity:
        """Coarse activity signal for a message recipient."""
        ...

    async def ensure_started(self, *, kind: str, target_id: str, project_id: str | None) -> bool:
        """Wake a supervisor-named session on demand. No-op (False) otherwise."""
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
    reconciler. The one write path is :meth:`ensure_started` for a
    supervisor-named session, which delegates to the provider registry —
    the same code path the reconciler uses.
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
        epoch: str = "",
        token_store=None,
    ):
        self._db = db
        self._providers = providers
        self._spec_builder = spec_builder
        self._harnesses = harness_registry
        self._config = config
        #: Async callable ``profile_id -> AgentProfile | None``. The
        #: supervisor profile ships in Task 6; tests inject a fake here.
        self._profiles_loader = profiles_loader
        #: Daemon epoch — launch provenance mirrored into the
        #: ``sessions`` row and into the spec builder so the reconciler
        #: sees the same value on both sides of the cold-start.
        self._epoch = epoch
        #: aq-surface Phase S2: optional session-scoped API bearer-token
        #: store.  When present, ``ensure_started`` mints a token and
        #: forwards it to the harness via ``AQ_API_TOKEN``.  Tests may
        #: omit; the harness falls back to no bearer (LOCAL_SCOPE).
        self._token_store = token_store

    # -- SessionManagerProto ------------------------------------------------

    async def activity(self, *, kind: str, target_id: str, project_id: str | None) -> Activity:
        row, handle = await self._resolve(kind=kind, target_id=target_id, project_id=project_id)
        if row is None or handle is None:
            return self._absent_signal(kind=kind, target_id=target_id)

        provider = self._providers.create(row.provider)
        try:
            running = await provider.is_running(handle)
        except Exception:  # provider misbehavior is not the engine's problem
            logger.debug("is_running failed for %s", row.name, exc_info=True)
            running = False
        if not running:
            return self._absent_signal(kind=kind, target_id=target_id)

        try:
            last = await provider.last_activity(handle)
        except Exception:
            logger.debug("last_activity failed for %s", row.name, exc_info=True)
            last = None
        if last is not None and (time.time() - last) <= _BUSY_WINDOW_SECONDS:
            return "busy"
        return "idle"

    async def ensure_started(self, *, kind: str, target_id: str, project_id: str | None) -> bool:
        # Only supervisor-named sessions are wake-on-demand. Task sessions
        # are launched by the task lifecycle; spawning one from the message
        # path would race the orchestrator and violate ownership.
        if kind != "session" or not target_id.startswith(_SUPERVISOR_NAME_PREFIX):
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
        derived_project = target_id[len(_SUPERVISOR_NAME_PREFIX) :] or project_id
        # The messaging address ``supervisor-global`` addresses the *global*
        # supervisor, not a per-project supervisor for a project literally
        # named "global". Fold it onto the global cold-start path so the
        # token is minted with project_id=None (admin scope) and the
        # session runs at the system vault root, not a phantom
        # ``projects/global`` directory. Runtime session name is still
        # ``n-supervisor--global`` (via ``named_session_name`` below), so
        # the reconciler adopts it on restart.
        is_global = derived_project == _GLOBAL_SUPERVISOR_SUFFIX
        if not derived_project:
            # A supervisor address always encodes a project
            # (``supervisor-<pid>``); an empty derivation means the caller
            # handed us a malformed address. Refuse before starting
            # anything so we never insert a phantom row with
            # ``project_id=""``.
            logger.debug(
                "supervisor address %r missing project id; refusing to start",
                target_id,
            )
            return False

        profile = await self._profiles_loader("supervisor")
        if profile is None:
            logger.warning("supervisor profile not found; cannot start session")
            return False

        harness_name = getattr(profile, "harness", None) or "claude"
        # For the global supervisor there is no per-project harness
        # registration to consult — resolve against the system-scoped
        # registry (project_id=None).
        harness_project = None if is_global else derived_project
        harness = self._harnesses.get(harness_name, project_id=harness_project)
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

        # Global supervisor runs at the system vault root; it has no
        # per-project directory.
        work_dir = (
            self._supervisor_work_dir(None)
            if is_global
            else self._supervisor_work_dir(derived_project)
        )
        if is_global:
            # Read the configured idle-timeout for observability. The
            # actual idle-teardown loop is a follow-up (there is no
            # ``idle_timeout`` field on ``SessionSpec`` yet); logging
            # here proves the config value is threaded through and
            # gives operators a single grep target when the loop lands.
            supervisor_cfg = getattr(self._config, "supervisor", None)
            global_cfg = getattr(supervisor_cfg, "global_", None)
            idle_timeout = getattr(global_cfg, "idle_timeout_seconds", 2700)
            logger.info(
                "cold-starting global supervisor (idle_timeout_seconds=%s)",
                idle_timeout,
            )
        # ``sessions.project_id`` is a NOT NULL FK to ``projects.id`` —
        # so persisting the global-supervisor row requires a stub
        # ``projects`` row named "global". Auto-create idempotently; the
        # token itself was minted with project_id=None (admin scope), so
        # this stub only exists to satisfy the FK and is never used to
        # narrow scope. If a real project happens to be named "global",
        # ``create_project`` is idempotent enough (INSERT OR IGNORE-style)
        # for it to be a no-op — see ``Database.create_project``.
        if is_global:
            try:
                from src.models import Project as _Project

                await self._db.create_project(
                    _Project(id=_GLOBAL_SUPERVISOR_SUFFIX, name="Global")
                )
            except IntegrityError:
                # Already exists — expected on every warm start.
                pass
            except Exception:
                logger.debug(
                    "failed to ensure 'global' project stub for global supervisor",
                    exc_info=True,
                )
        if not work_dir:
            # No vault_root configured and no project_id → nowhere sensible
            # to run the supervisor. Better to skip than to pass an empty
            # cwd to the harness and get a confusing downstream failure.
            logger.debug(
                "supervisor work_dir is empty (vault_root unset, project_id=%r); refusing to start",
                derived_project,
            )
            return False
        # `claude --session-id` rejects bare hex — must be dashed.
        session_id = str(uuid.uuid4())
        instance_token = uuid.uuid4().hex[:12]

        # aq-surface Phase S2: mint the session's API bearer token.  Empty
        # string when no store is wired (tests) — harness treats absent
        # value as no-token.  task_id is None: named sessions are not
        # bound to a task.
        api_token = ""
        if self._token_store is not None:
            try:
                # Global supervisor gets an admin-scope token
                # (elevated + project_id=None); per-project supervisors
                # get an elevated token narrowed to their project.
                mint_project = None if is_global else derived_project
                api_token = await self._token_store.mint(
                    session_id=session_id,
                    task_id=None,
                    project_id=mint_project,
                    # Supervisor is the trusted operator — elevate so it
                    # can run every ``aq`` command (e.g.
                    # ``project_create``, ``task_create``, ``session_*``)
                    # on behalf of the user. Per-project scope is still
                    # enforced by ``check_command_scope`` when
                    # ``project_id`` is set; global admin (project_id=
                    # None) is loopback-restricted in the middleware.
                    elevated=True,
                )
            except Exception:
                # Non-fatal: starting the session with an empty api_token
                # is better than refusing to start at all — the agent will
                # 401 on its own `aq` calls, which is loud enough to
                # diagnose.  Log at WARNING with session id so the failure
                # is visible in production instead of buried at DEBUG.
                logger.warning(
                    "token mint failed for supervisor %s (session_id=%s); "
                    "session will start with empty api_token",
                    target_id,
                    session_id,
                    exc_info=True,
                )

        # Let the spec builder derive the provider session name from the
        # profile+project convention (``n-supervisor--<pid>``). The
        # messaging *address* remains ``supervisor-<pid>``; the lens
        # translates the two at every runtime-facing edge (see
        # :func:`_resolve_runtime_session_name`). Naming the runtime
        # session ``n-...`` is what lets
        # :class:`~src.sessions.reconciler.SessionReconciler` adopt it
        # after a daemon restart — adoption only scans ``s-``/``n-``
        # prefixes.
        spec = self._spec_builder.build_named_spec(
            profile=profile,
            harness=harness,
            project_id=derived_project,
            work_dir=work_dir,
            session_id=session_id,
            instance_token=instance_token,
            epoch=self._epoch,
            api_token=api_token,
        )
        try:
            await provider.start(spec)
        except Exception:
            logger.exception(
                "failed to start supervisor session %s (project=%s)",
                target_id,
                derived_project,
            )
            return False

        # Persist the sessions row so subsequent lens ops (``activity``,
        # ``nudge``) can resolve the just-started supervisor.  Without this
        # step the reconciler would eventually adopt the orphan on its next
        # tick, but the delivery engine's *same-pass* nudge would already
        # have failed to find a row.  Mirrors the ``provider.start`` →
        # ``db.create_session`` ordering in
        # ``src/orchestrator/execution.py`` (crash between start and row
        # insert leaves an orphan the reconciler can heal; the reverse
        # would leave a phantom row).
        try:
            await self._db.create_session(
                SessionRecord(
                    # Mirror execution.py canon: the row id *is* the dashed
                    # session id we handed the harness via ``--session-id``.
                    # That single identity is what makes the row, the
                    # harness's own conversation id and the resume key line
                    # up on restart.
                    id=session_id,
                    project_id=derived_project,
                    profile_id=getattr(profile, "id", "") or "supervisor",
                    harness=harness.id,
                    provider=provider_name,
                    name=spec.session_name,
                    lifecycle="named",
                    work_dir=work_dir,
                    epoch=self._epoch,
                    instance_token=instance_token,
                    started_at=time.time(),
                    # ``--session-id`` pinned the harness's conversation id
                    # to ours, so the resume key *is* the session id — same
                    # invariant execution.py relies on.
                    session_key=session_id,
                    state="running",
                    # Explicit rather than defaulted: a cold start is the
                    # clearest statement of intent there is, and the
                    # reconciler's up-convergence reads this column.
                    desired_state="running",
                )
            )
        except IntegrityError:
            # A racing reconciler / concurrent lens call may have inserted
            # the row already — either way the process is alive; the
            # engine's next resolve will find it. Other failures propagate
            # to ensure_started's caller.
            logger.debug(
                "supervisor session row insert lost the race (already exists)",
                exc_info=True,
            )
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

        row, _handle = await self._resolve(kind=kind, target_id=target_id, project_id=project_id)
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

    @staticmethod
    def _absent_signal(*, kind: str, target_id: str) -> Activity:
        """ "absent" for tasks and plain session names; "sleeping" only for
        supervisor-named sessions (wake-able by design)."""
        if kind == "session" and target_id.startswith(_SUPERVISOR_NAME_PREFIX):
            return "sleeping"
        return "absent"

    async def _resolve(
        self, *, kind: str, target_id: str, project_id: str | None
    ) -> tuple[object | None, SessionHandle | None]:
        """Map (kind, target_id, project_id) → (session row, handle).

        Read-only. The reconciler owns writes to ``sessions``.
        """
        row = None
        if kind == "task":
            row = await self._db.get_session_by_name(task_session_name(target_id))
        elif kind == "session":
            # ``target_id`` is a bare session address — resolve via the
            # by-name index directly. Callers (delivery engine) never
            # invent this; it comes off ``messages.to_id``. The supervisor
            # is addressed as ``supervisor-<pid>`` but the runtime session
            # row is named ``n-supervisor--<pid>`` (so the reconciler can
            # adopt it); translate here.
            runtime_name = _resolve_runtime_session_name(kind, target_id)
            row = await self._db.get_session_by_name(runtime_name)
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
