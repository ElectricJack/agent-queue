"""Core authorization, independent of transport capability audit/off settings."""

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select

from src.commands.principal import ExecutionPrincipal, PrincipalKind, matches_session_instance
from src.database.tables import projects, sessions, tasks
from src.records.models import RecordError


@dataclass(frozen=True)
class RecordAccess:
    principal: ExecutionPrincipal
    actor_key: str
    project_id: str
    claim_epoch: int | None

    @property
    def supervisor(self) -> bool:
        return self.principal.kind == PrincipalKind.LOCAL or self.principal.elevated

    def task_visible(self, task_id: str) -> bool:
        return self.supervisor or task_id == self.principal.task_id

    def editable(self, record: dict, snapshot: dict | None = None) -> None:
        if record["kind"] == "task":
            if not self.task_visible(record["task_id"]):
                raise RecordError("record.not_found")
            return
        if snapshot and snapshot["verification"] != "unverified":
            # K05 must revoke authority/verify/propose atomically. Its absence
            # cannot turn a protected row inserted by another version editable.
            raise RecordError("knowledge.operation_unavailable")
        if not self.supervisor and record["created_by"] != self.actor_key:
            raise RecordError("record.forbidden", "Only the author may revise this finding")


async def authorize_on(
    conn,
    config,
    principal: ExecutionPrincipal,
    operation: str,
    project_id: str,
    *,
    write: bool = False,
    claim_epoch: int | None = None,
) -> RecordAccess:
    if not config.enabled:
        raise RecordError("knowledge.disabled")
    if not isinstance(project_id, str) or not project_id or project_id == "*":
        raise RecordError("knowledge.operation_unavailable", "Explicit project scope required")
    if (
        principal is None
        or principal.kind not in (PrincipalKind.LOCAL, PrincipalKind.SESSION)
        or principal.unresolved
    ):
        raise RecordError("record.forbidden")
    if principal.kind != PrincipalKind.LOCAL:
        if not principal.policy.allows_aq_command(operation):
            raise RecordError("record.forbidden")
        if principal.project_id != project_id:
            raise RecordError("record.not_found")
    if project_id not in config.enabled_projects:
        raise RecordError("knowledge.disabled")
    if not await conn.scalar(select(projects.c.id).where(projects.c.id == project_id)):
        raise RecordError("record.not_found")
    if write and not config.writes_enabled:
        raise RecordError("knowledge.read_only")
    if write and config.legacy_memory_mode != "disabled":
        raise RecordError("knowledge.legacy_writer_conflict")
    if principal.kind == PrincipalKind.LOCAL:
        return RecordAccess(principal, "local-operator", project_id, None)

    session = (
        (await conn.execute(select(sessions).where(sessions.c.id == principal.session_id)))
        .mappings()
        .first()
    )
    if (
        not session
        or session["state"] not in ("running", "draining")
        or session["ended_at"] is not None
        or not matches_session_instance(principal, session["instance_token"])
        or session["profile_id"] != principal.profile_id
        or session["project_id"] != project_id
    ):
        raise RecordError("record.forbidden")
    instance = sha256(session["instance_token"].encode()).hexdigest()[:16]
    actor = f"session:{session['id']}:instance:{instance}"
    if not principal.elevated:
        if not principal.task_id or session["task_id"] != principal.task_id:
            raise RecordError("record.forbidden")
        task = (
            (await conn.execute(select(tasks).where(tasks.c.id == principal.task_id)))
            .mappings()
            .first()
        )
        if not task or task["project_id"] != project_id:
            raise RecordError("record.forbidden")
        if write and (
            type(claim_epoch) is not int
            or task["claim_epoch"] != claim_epoch
            or (session["lifecycle"] == "pool" and session["last_claim_epoch"] != claim_epoch)
            or not session["agent_id"]
            or task["assigned_agent_id"] != session["agent_id"]
            or task["status"] != "IN_PROGRESS"
        ):
            raise RecordError("record.stale_claim")
        actor += f":task:{task['id']}:claim:{task['claim_epoch']}"
    return RecordAccess(principal, actor, project_id, claim_epoch)
