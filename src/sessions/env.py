"""Session environment construction — the ``AQ_*`` identity markers.

Every session carries nine ``AQ_*`` variables.  They are the system's
identity and liveness substrate: adoption scans the process table for
``AQ_SESSION_ID`` (never PID files, never session names — names get reused
and PIDs get recycled), kills are fenced on ``AQ_INSTANCE_TOKEN``, and the
``aq`` CLI inside the session reaches its daemon through ``AQ_API_URL`` /
``AQ_API_TOKEN``.

``AQ_TASK_ID`` and ``AQ_SESSION_ID`` are the exact names ``aq prime`` /
``aq handoff`` fall back to (``src/cli/agent_surface.py``) — this module is
the other half of that handshake, so the names are load-bearing, not
cosmetic.

A push launch adds one more marker outside the fixed nine: ``AQ_CLAIM_EPOCH``,
set via ``extra_env`` when the launch bumps the task's claim epoch and joins
the fence (swarm-work-model §10) — the agent's writes are then checked
against it the same way a pool session's are.

A pool launch (§11.2) adds its own ``extra_env`` markers instead:
``AQ_SESSION_KIND=pool``, ``AQ_AGENT_ID``, ``AQ_PROFILE_ID``, and
``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` so commits made by a long-lived pool
worker attribute to its profile rather than the operator's own git identity.
See :func:`src.sessions.spec.SessionSpecBuilder.build_pool_spec`.

Scrubbing is **not** implemented here.  :func:`src.env_scrub.scrub_env` owns
the policy (trust-and-ops R6); this module supplies the ``explicit`` map it
merges last and passes the daemon config through so the
``security.env_scrub_enabled`` kill switch and ``security.env_allowlist``
are actually honoured at the launch site.  ``CLAUDECODE`` /
``CLAUDE_CODE_ENTRYPOINT`` stripping comes free from ``STRIP_ALWAYS`` there
— this module does not repeat it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from src.env_scrub import is_harness_session_marker, scrub_env, scrub_env_from_config

logger = logging.getLogger(__name__)

__all__ = [
    "ADOPTION_MARKER",
    "AQ_MARKER_KEYS",
    "DB_ISOLATION_KEYS",
    "SCRATCH_DB_RELPATH",
    "STARTUP_PROMPT_DELIVERED",
    "build_session_env",
    "session_db_isolation",
    "session_markers",
]

#: The env var whose presence in ``/proc/<pid>/environ`` identifies a
#: process as one of ours during the adoption scan.
ADOPTION_MARKER = "AQ_SESSION_ID"

#: Set to ``"1"`` when the bootstrap prompt already rode argv on this start,
#: so the harness ``SessionStart`` hook suppresses a duplicate prime.
#: Owned by ``src/prime/hook_envelopes.py``; named here because the session
#: env is where it is set.
STARTUP_PROMPT_DELIVERED = "AQ_STARTUP_PROMPT_DELIVERED"

#: The nine identity markers, in the order design §3 lists them.
AQ_MARKER_KEYS: tuple[str, ...] = (
    "AQ_SESSION_ID",
    "AQ_TASK_ID",
    "AQ_PROJECT_ID",
    "AQ_PROFILE",
    "AQ_DAEMON_EPOCH",
    "AQ_INSTANCE_TOKEN",
    "AQ_WORK_DIR",
    "AQ_API_URL",
    "AQ_API_TOKEN",
)


#: Where a session's scratch database lives, relative to its work dir.
#: ``.aq/`` is already the session's private corner of the worktree (it holds
#: ``claim.json``) and the repo gitignores it, so nothing here can be
#: committed by accident.
SCRATCH_DB_RELPATH = os.path.join(".aq", "scratch.db")

#: The database-isolation block every session carries, on top of the nine
#: identity markers.  ``AQ_DB_SCOPE`` is the guard
#: (:mod:`src.database.migration_guard`); the two URL overrides are the
#: somewhere-else it points db tooling at.
DB_ISOLATION_KEYS: tuple[str, ...] = (
    "AQ_DB_SCOPE",
    "AQ_DATABASE_URL",
    "AGENT_QUEUE_DB",
)


def session_db_isolation(work_dir: str) -> dict[str, str]:
    """Env that keeps a session's database tooling off the production DB.

    Two independent halves, because either alone has a hole:

    * ``AQ_DB_SCOPE=worker`` makes :func:`src.database.migration_guard.
      current_scope` resolve to ``worker`` for *everything* the session
      launches — pytest, a stray ``alembic upgrade``, even an ``aq start``
      inside the slot — so a migration against the production URL is refused
      rather than applied.  This is the half that closes the 2026-09-02
      incident, where an unmerged branch's revision landed in production's
      ``alembic_version`` and the daemon then refused to boot.
    * ``AQ_DATABASE_URL`` / ``AGENT_QUEUE_DB`` point the direct-DB CLI paths
      at a per-slot scratch SQLite file instead of ``config.yaml``'s URL, so
      the ordinary case never even reaches the guard.

    Set as ``explicit`` env, so an operator who pins one of these in a
    harness file or via ``extra_env`` still wins.
    """
    scratch = os.path.join(work_dir, SCRATCH_DB_RELPATH) if work_dir else ""
    isolation = {"AQ_DB_SCOPE": "worker"}
    if scratch:
        isolation["AQ_DATABASE_URL"] = scratch
        isolation["AGENT_QUEUE_DB"] = scratch
    return isolation


def session_markers(
    *,
    session_id: str,
    task_id: str | None,
    project_id: str,
    profile_id: str,
    epoch: str,
    instance_token: str,
    work_dir: str,
    api_url: str,
    api_token: str,
) -> dict[str, str]:
    """Return just the ``AQ_*`` marker map.

    ``AQ_TASK_ID`` is omitted (not set empty) for named sessions: an empty
    value would make ``aq prime`` resolve to the empty task id rather than
    fall through to its "no task in scope" branch.
    """
    markers = {
        "AQ_SESSION_ID": session_id,
        "AQ_PROJECT_ID": project_id,
        "AQ_PROFILE": profile_id,
        "AQ_DAEMON_EPOCH": epoch,
        "AQ_INSTANCE_TOKEN": instance_token,
        "AQ_WORK_DIR": work_dir,
        "AQ_API_URL": api_url,
        "AQ_API_TOKEN": api_token,
    }
    if task_id:
        markers["AQ_TASK_ID"] = task_id
    return markers


def build_session_env(
    *,
    session_id: str,
    task_id: str | None,
    project_id: str,
    profile_id: str,
    epoch: str,
    instance_token: str,
    work_dir: str,
    api_url: str,
    api_token: str,
    harness_env: Mapping[str, str] | None = None,
    config=None,
    base: Mapping[str, str] | None = None,
    prompt_delivered: bool = False,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the full child environment for one session launch.

    Layering, outermost first: the daemon environment (scrubbed) → the
    database-isolation block (:func:`session_db_isolation`) → the harness's
    own ``env`` map → the ``AQ_*`` markers → ``extra_env``.
    Everything from the second layer inward is ``explicit`` as far as
    :func:`~src.env_scrub.scrub_env` is concerned, so an operator who names
    a key in a harness file means it and it survives.  ``extra_env`` is
    applied last, after the markers and before scrubbing — e.g.
    ``AQ_CLAIM_EPOCH`` for a push launch joining the claim fence.

    ``config`` is the daemon :class:`~src.config.AppConfig`.  Passing it is
    what makes ``security.env_scrub_enabled`` / ``security.env_allowlist``
    reachable — omitting it silently falls back to shipped defaults, which
    is exactly the bug lane 1C shipped and then fixed.  Call sites must pass
    it; ``None`` is for unit tests that do not exercise the policy.
    """
    explicit: dict[str, str] = {}
    explicit.update(session_db_isolation(work_dir))
    if harness_env:
        harness_explicit = {str(k): str(v) for k, v in harness_env.items()}
        # A harness file cannot restore a marker inherited from its enclosing
        # CLI.  This guard deliberately applies only to harness-file input:
        # the spec builder may itself set a provider's effort variable as an
        # intentional setting for the freshly launched session.
        for key in tuple(harness_explicit):
            if is_harness_session_marker(key):
                logger.warning(
                    "Harness env sets %s — ignored (it makes a nested CLI think "
                    "it is already inside a harness session)",
                    key,
                )
                harness_explicit.pop(key, None)
        explicit.update(harness_explicit)
    explicit.update(
        session_markers(
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            profile_id=profile_id,
            epoch=epoch,
            instance_token=instance_token,
            work_dir=work_dir,
            api_url=api_url,
            api_token=api_token,
        )
    )
    if prompt_delivered:
        explicit[STARTUP_PROMPT_DELIVERED] = "1"
    if extra_env:
        explicit.update({str(k): str(v) for k, v in extra_env.items()})

    # A daemon can itself be running inside an AQ session (notably in tests
    # and when a supervisor launches a named child).  Never inherit that
    # parent's identity into a child: named/prompt-less sessions deliberately
    # omit some markers, and stale inherited values would put them in the
    # wrong task scope.  The current launch's markers above are authoritative.
    inherited = {
        key: value
        for key, value in (base if base is not None else os.environ).items()
        if not key.startswith("AQ_")
    }

    if config is not None:
        result = scrub_env_from_config(config, base=inherited, explicit=explicit)
    else:
        result = scrub_env(inherited, explicit=explicit)

    if result.dropped:
        logger.debug(
            "Session %s env scrub withheld %d key(s): %s",
            session_id,
            len(result.dropped),
            ", ".join(result.dropped),
        )
    return result.env
