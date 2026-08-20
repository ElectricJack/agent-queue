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
from collections.abc import Mapping

from src.env_scrub import STRIP_ALWAYS, scrub_env, scrub_env_from_config

logger = logging.getLogger(__name__)

__all__ = [
    "AQ_MARKER_KEYS",
    "ADOPTION_MARKER",
    "STARTUP_PROMPT_DELIVERED",
    "build_session_env",
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
) -> dict[str, str]:
    """Build the full child environment for one session launch.

    Layering, outermost first: the daemon environment (scrubbed) → the
    harness's own ``env`` map → the ``AQ_*`` markers.  Everything from the
    second layer inward is ``explicit`` as far as
    :func:`~src.env_scrub.scrub_env` is concerned, so an operator who names
    a key in a harness file means it and it survives.

    ``config`` is the daemon :class:`~src.config.AppConfig`.  Passing it is
    what makes ``security.env_scrub_enabled`` / ``security.env_allowlist``
    reachable — omitting it silently falls back to shipped defaults, which
    is exactly the bug lane 1C shipped and then fixed.  Call sites must pass
    it; ``None`` is for unit tests that do not exercise the policy.
    """
    explicit: dict[str, str] = {}
    if harness_env:
        explicit.update({str(k): str(v) for k, v in harness_env.items()})
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

    # A harness file naming a STRIP_ALWAYS key would defeat the nested-CLI
    # guard, and no legitimate harness does.  Drop it with a warning rather
    # than letting `explicit` win, because the failure mode (a Claude CLI
    # that believes it is already inside a session) is silent and confusing.
    for key in STRIP_ALWAYS:
        if key in explicit:
            logger.warning(
                "Harness env sets %s — ignored (it makes a nested CLI think "
                "it is already inside a Claude Code session)",
                key,
            )
            explicit.pop(key, None)

    if config is not None:
        result = scrub_env_from_config(config, base=base, explicit=explicit)
    else:
        result = scrub_env(base, explicit=explicit)

    if result.dropped:
        logger.debug(
            "Session %s env scrub withheld %d key(s): %s",
            session_id,
            len(result.dropped),
            ", ".join(result.dropped),
        )
    return result.env
