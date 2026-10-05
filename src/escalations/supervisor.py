"""Bounded watchdog for durable messages awaiting their logical supervisor."""

from __future__ import annotations

import logging
import time

from src.escalations.facts import OPEN_STATES
from src.sessions.spec import named_session_name

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
        # A later reply on the same thread can be a new delivery outage.
        # Anchor the incident to the notice, rather than to its conversation.
        return str(message["id"]).removeprefix("msg-")

    async def _emit(self, event: str, incident: dict) -> None:
        try:
            await self.bus.emit(
                event,
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

    async def tick(self, now: float | None = None) -> int:
        now = time.time() if now is None else float(now)
        # Include younger pending notices when checking recovery: draining
        # just the oldest notice must not close and recreate the same outage.
        rows = await self.db.list_overdue_supervisor_incident_messages(now)
        groups: dict[tuple[str, str], list[dict]] = {}
        for message in rows:
            groups.setdefault((message["project_id"], message["body_kind"]), []).append(message)
        open_incidents = await self.db.list_escalations(
            source_kind="supervisor_delivery", states=tuple(OPEN_STATES), limit=500
        )
        sessions: dict[str, float | None] = {}

        async def started_at(owner: str) -> float | None:
            if owner not in sessions:
                name = named_session_name("supervisor", owner.removeprefix("supervisor-"))
                live = await self.db.list_sessions(
                    name=name, state="running", desired_state="running", limit=1
                )
                sessions[owner] = live[0].started_at if live else None
            return sessions[owner]

        still_open: set[tuple[str, str]] = set()
        for incident in open_incidents:
            group = (incident["project_id"], incident["source_identity"].partition(":")[0])
            # Installed incidents can still carry the old high severity.
            if incident["severity"] != "low":
                changed = await self.db.transition_escalation(
                    incident["id"],
                    expected_revision=incident["revision"],
                    new_state=incident["state"],
                    severity="low",
                    now=now,
                )
                if changed is None:
                    still_open.add(group)
                    continue
                incident = changed
                await self._emit("escalation.updated.v1", incident)
            started = await started_at(incident["supervisor_owner"])
            drained = group not in groups
            restarted = started is not None and started >= incident["created_at"]
            if drained or restarted:
                resolved = await self.db.resolve_escalation_on_recovery(
                    incident["id"],
                    expected_revision=incident["revision"],
                    source_kind="supervisor_delivery",
                    terminal_outcome=(
                        "Supervisor notices delivered; delivery backlog cleared."
                        if drained
                        else "Supervisor session started; delivery path recovered."
                    ),
                    terminal_evidence={
                        "project_id": group[0],
                        "body_kind": group[1],
                        "pending_notices": len(groups.get(group, ())),
                        "supervisor_started_at": started,
                    },
                    now=now,
                )
                if resolved is not None:
                    await self._emit("escalation.updated.v1", resolved)
                else:
                    still_open.add(group)
            else:
                still_open.add(group)

        cutoff = now - self._timeout_seconds()
        created_count = 0
        for group, pending in groups.items():
            message = pending[0]  # query order is oldest first, stable across restarts
            if group in still_open or message["created_at"] > cutoff:
                continue
            started = await started_at(message["to_id"])
            if started is not None and started >= message["created_at"]:
                # A newly started supervisor is draining this outage's queue.
                continue
            source = self._source_identity(message)
            incident, created = await self.db.create_escalation(
                id=f"escalation-supervisor-delivery-{group[0]}-{group[1]}-{source}",
                project_id=message["project_id"],
                task_id=None,
                source_kind="supervisor_delivery",
                source_identity=f"{message['body_kind']}:{source}",
                incident_key=f"supervisor-unavailable:{message['body_kind']}:{source}",
                supervisor_owner=message["to_id"],
                summary=f"Supervisor delivery unavailable for project {message['project_id']}",
                investigation=(
                    f"{len(pending)} internal {message['body_kind']} notices are pending; "
                    f"oldest notice {message['id']} remained undelivered "
                    f"for at least {self._timeout_seconds() / 60:g} minutes."
                ),
                decision_requested=(
                    "Restore or inspect the project supervisor delivery path. This operational "
                    "notice does not approve, reject, or reinterpret the underlying work."
                ),
                choices=None,
                severity="low",
                supervisor_delivery_body_kind=message["body_kind"],
                now=now,
            )
            if not created:
                continue
            created_count += 1
            await self._emit("escalation.created.v1", incident)
        return created_count
