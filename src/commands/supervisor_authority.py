"""Shared authority checks for integration controls run by supervisors."""

from __future__ import annotations

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal


LIVE_SESSION_STATES = frozenset({"starting", "running", "draining"})


async def integration_operator(db, project_id: str | None) -> tuple[str | None, str | None]:
    """Return an audit principal label or a refusal for an integration control.

    The loopback operator is always admitted.  A session principal must be an
    elevated, live, named supervisor for this project (or the global
    supervisor) before it may run a control.  The database row is consulted
    rather than trusting the request scope alone, so a stale token cannot keep
    operating after its supervisor has stopped.
    """
    principal = current_principal() or TRUSTED_LOCAL
    if principal.kind is PrincipalKind.LOCAL:
        return "human:local-operator", None
    if principal.kind is not PrincipalKind.SESSION or not principal.elevated:
        return None, "a local operator or live supervisor session is required"

    session_id = principal.session_id
    row = await db.get_session(session_id) if session_id else None
    if (
        row is None
        or row.profile_id != "supervisor"
        or row.lifecycle != "named"
        or row.state not in LIVE_SESSION_STATES
        or row.desired_state != "running"
    ):
        return None, "a live named supervisor session is required"
    if principal.project_id is not None and principal.project_id != project_id:
        return None, "integration control belongs to another project"
    if row.project_id is not None and row.project_id != project_id:
        return None, "integration control belongs to another project"
    return f"supervisor session:{row.id}", None
