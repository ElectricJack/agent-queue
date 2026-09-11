"""Rules for profiles that may back an executable task.

Supervisor sessions are named control-plane sessions. They deliberately use
the same profile storage as workers, but must never become a route in the task
queue: a task needs a worker session and a workspace lifecycle, whereas the
supervisor is woken to coordinate work without holding either.
"""

from __future__ import annotations

from typing import Any


SUPERVISOR_PROFILE_ID = "supervisor"


def is_supervisor_profile(profile: Any | str | None) -> bool:
    """Whether *profile* denotes the non-executable supervisor control plane."""
    if isinstance(profile, str):
        return profile == SUPERVISOR_PROFILE_ID
    return bool(
        profile
        and (
            getattr(profile, "id", None) == SUPERVISOR_PROFILE_ID
            or getattr(profile, "runtime", "") == "supervisor"
        )
    )


def task_execution_profile_error(profile: Any | str | None) -> str | None:
    """Return the operator-facing refusal for a non-executable supervisor."""
    if not is_supervisor_profile(profile):
        return None
    profile_id = profile if isinstance(profile, str) else getattr(profile, "id", "supervisor")
    return (
        f"profile '{profile_id}' is a supervisor control-plane profile and cannot execute "
        "queued tasks; select an eligible worker profile instead"
    )


__all__ = ["SUPERVISOR_PROFILE_ID", "is_supervisor_profile", "task_execution_profile_error"]
