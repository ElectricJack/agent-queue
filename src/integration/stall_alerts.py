"""Durable stall incidents, called only by the daemon's train command."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import messages
from src.doctor.integration_checks import stalled_train_batches
from src.doctor.models import DoctorContext

STALL_AFTER_SECONDS = 60 * 60


async def alert_stalled_batches(handler, *, now):
    ctx = DoctorContext(db=handler.db, config=handler.config, handler=handler)
    findings = await stalled_train_batches(ctx, now=now, alert_after_seconds=STALL_AFTER_SECONDS)
    for finding in findings:
        batch_id, project_id = finding["batch_id"], finding["project_id"]
        incident_id = "escalation-train-stall-" + hashlib.sha256(batch_id.encode()).hexdigest()[:24]
        summary = finding["detail"]
        decision = (f"Inspect aq integration status {project_id}; restore repository health or "
                    "choose a batch repair, resume or abort after checking its frozen inputs. "
                    "The alert does not pause delivery; cancel it if the train has recovered.")
        # Incident, Discord outbox and inbox notice are one transaction. The
        # source/incident key survives restarts and changing error strings.
        async with handler.db.immediate() as conn:
            incident, created = await handler.db.create_escalation(
                conn=conn, id=incident_id, project_id=project_id,
                source_kind="integration_train_stall", source_identity=batch_id,
                incident_key=f"train-stalled:{batch_id}",
                supervisor_owner=f"supervisor-{project_id}",
                task_title=f"Restore delivery of batch {batch_id}", summary=summary,
                investigation=json.dumps(finding, sort_keys=True)[:8000],
                decision_requested=decision, severity="high", now=now,
            )
            if created:
                await conn.execute(pg_insert(messages).values(
                    id="msg-" + incident_id, project_id=project_id,
                    from_kind="system", from_id="integration-train", to_kind="session",
                    to_id=incident["supervisor_owner"], subject=summary,
                    body=f"{summary}\n{incident['investigation']}\n{decision}\nIncident: {incident_id}",
                    thread_id=incident_id, priority=100, created_at=now, archive_after_inject=1,
                    body_kind="integration_train_stalled",
                ).on_conflict_do_nothing(index_elements=[messages.c.id]))
