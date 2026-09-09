"""Bounded watchdog for durable messages awaiting their logical supervisor."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_MINUTES = 15


class SupervisorDeliveryWatchdog:
    """Publish one operational incident when supervisor delivery stays unavailable.

    The watchdog reports availability only. Its ``supervisor_delivery`` source
    kind is intentionally unsupported by ``escalation_apply_reply``, so neither
    the watchdog nor a reply to its notice can approve the underlying work.
    """

    def __init__(self, db, bus, config):
        self.db, self.bus, self.config = db, bus, config

    def _timeout_seconds(self) -> float:
        discord = getattr(self.config, "discord", None)
        escalation = getattr(discord, "escalation", None)
        minutes = getattr(
            escalation, "supervisor_delivery_timeout_minutes", _DEFAULT_TIMEOUT_MINUTES
        )
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
            minutes = _DEFAULT_TIMEOUT_MINUTES
        return float(minutes) * 60

    @staticmethod
    def _source_identity(message: dict) -> str:
        if message["body_kind"] == "escalation_reply" and message.get("thread_id"):
            return str(message["thread_id"])
        return str(message["id"]).removeprefix("msg-")

    async def _task_snapshot(self, message: dict) -> tuple[str | None, str | None, str | None]:
        task_id = None
        if message["body_kind"] == "agent_question":
            question = await self.db.get_agent_question(self._source_identity(message))
            task_id = question.get("task_id") if question else None
        elif message["body_kind"] == "task_recovery":
            subject = str(message.get("subject") or "")
            if subject.startswith("Task recovery: "):
                task_id = subject.removeprefix("Task recovery: ")
        elif message["body_kind"] == "escalation_reply":
            incident = await self.db.get_escalation(self._source_identity(message))
            task_id = incident.get("task_id") if incident else None
        task = await self.db.get_task(task_id) if task_id else None
        if task is None:
            return task_id, None, None
        status = getattr(task.status, "value", str(task.status))
        return task.id, task.title, status

    async def tick(self, now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        rows = await self.db.list_overdue_supervisor_incident_messages(
            now - self._timeout_seconds()
        )
        created_count = 0
        for message in rows:
            source = self._source_identity(message)
            task_id, task_title, task_status = await self._task_snapshot(message)
            incident, created = await self.db.create_escalation(
                id=f"escalation-supervisor-delivery-{message['body_kind']}-{source}",
                project_id=message["project_id"],
                task_id=task_id,
                source_kind="supervisor_delivery",
                source_identity=f"{message['body_kind']}:{source}",
                incident_key=f"supervisor-unavailable:{message['body_kind']}:{source}",
                supervisor_owner=message["to_id"],
                task_title=task_title,
                task_status=task_status,
                summary=f"Supervisor delivery unavailable for project {message['project_id']}",
                investigation=(
                    f"Internal {message['body_kind']} notice {message['id']} remained undelivered "
                    f"for at least {self._timeout_seconds() / 60:g} minutes."
                ),
                decision_requested=(
                    "Restore or inspect the project supervisor delivery path. This operational "
                    "notice does not approve, reject, or reinterpret the underlying work."
                ),
                choices=None,
                severity="high",
                now=now,
            )
            if not created:
                continue
            created_count += 1
            try:
                await self.bus.emit(
                    "escalation.created.v1",
                    {
                        "version": 1,
                        "escalation_id": incident["id"],
                        "project_id": incident["project_id"],
                        "task_id": incident.get("task_id"),
                        "source_kind": incident["source_kind"],
                        "source_identity": incident["source_identity"],
                        "incident_key": incident["incident_key"],
                        "state": incident["state"],
                        "revision": incident["revision"],
                    },
                )
            except Exception:
                logger.warning("supervisor delivery watchdog event failed", exc_info=True)
        return created_count
