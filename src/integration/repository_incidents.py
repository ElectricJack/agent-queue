"""Retained-cache admission holds and incident notices owned by CommandHandler.

Use the existing system-config ledger and escalation/message outbox. The hold
is separate from project status, operator decisions and frozen batch intent.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import escalations, messages, projects, repos, system_config
from src.git.manager import GitError, safe_git_detail


def _prefix(project_id):
    return "retained_repository_recovery:" + hashlib.sha256(project_id.encode()).hexdigest() + ":"


def incident_key(state):
    return _prefix(state["project_id"]) + hashlib.sha256(state["cache"].encode()).hexdigest()


async def admission_holds_on(conn, project_id):
    rows = (await conn.execute(select(system_config.c.value).where(
        system_config.c.key.startswith(_prefix(project_id)),
    ))).scalars()
    return tuple(state for value in rows if (state := json.loads(value))["status"] != "recovered")


async def admission_holds(db, project_id):
    async with db._engine.connect() as conn:
        return await admission_holds_on(conn, project_id)


async def record_incident(handler, state, *, load=False):
    """Daemon-only state port; serialization shares new-batch admission's project lock."""
    if not state["project_id"] or not state["repository_id"]:
        raise GitError("retained recovery requires project/repository identity")
    state = deepcopy(state)
    key, now = incident_key(state), time.time()
    async with handler.db.immediate() as conn:
        project = await conn.scalar(select(projects.c.id).where(
            projects.c.id == state["project_id"],
        ).with_for_update())
        repository = await conn.scalar(select(repos.c.id).where(
            repos.c.id == state["repository_id"], repos.c.project_id == project,
        ))
        if project is None or repository is None:
            raise GitError("retained recovery repository binding changed")
        previous = await conn.scalar(select(system_config.c.value).where(
            system_config.c.key == key,
        ).with_for_update())
        previous = json.loads(previous) if previous else None
        if previous and previous["status"] != "recovered":
            if previous["id"] != state["id"]:
                if not load:
                    raise GitError("another retained recovery incident owns admission")
                state = previous
            else:
                state["attempts"] = max(previous["attempts"], state["attempts"])
                # The file's write-ahead move ledger may be newer than PostgreSQL on restart.
                moves = {item["backup"]: item for item in previous["moves"]}
                moves.update({item["backup"]: item for item in state["moves"]})
                state["moves"] = list(moves.values())
                if previous["status"] == "exhausted":
                    state["status"] = "exhausted"
        if state["attempts"] > 2:
            raise GitError("retained recovery attempt budget exceeded")
        state["updated_at"] = now
        await conn.execute(pg_insert(system_config).values(key=key, value=json.dumps(state)).
                           on_conflict_do_update(index_elements=[system_config.c.key],
                                                 set_={"value": json.dumps(state)}))
        if state["status"] in {"failed", "exhausted"}:
            incident_id = "escalation-retained-" + state["id"]
            summary = (f"Retained Git cache recovery {state['status']} for {project} "
                       f"(attempt {state['attempts']}/2)")
            details = safe_git_detail(json.dumps({
                "checks": state["checks"], "error": state.get("error"),
            }), limit=4000)
            identity = safe_git_detail(json.dumps({
                "cache": state["cache"], "backup": state["backup"],
                "attempts": state["attempts"], "incident_id": state["id"],
            }), limit=2000)
            moved = json.dumps([item["oid"] for item in state["moves"]])[:1500]
            investigation = (f"{identity}\nMoved OIDs (see manifest for full list): {moved}\n"
                             f"Scrubbed diagnostics: {details}")
            decision = ("Inspect the retained recovery journal and preserved objects. New project "
                        "batches remain held until full fsck and guarded local-only AQ publication "
                        "succeed. Existing owners, frozen inputs and operator pauses are unchanged.")
            incident, created = await handler.db.create_escalation(
                conn=conn, id=incident_id, project_id=project,
                source_kind="retained_repository_recovery",
                source_identity=state["cache"] + "#" + state["id"],
                incident_key="retained-recovery:" + state["id"],
                supervisor_owner=f"supervisor-{project}", task_title=summary, summary=summary,
                investigation=investigation, decision_requested=decision, severity="high", now=now,
            )
            if created:
                await conn.execute(pg_insert(messages).values(
                    id="msg-" + incident_id, project_id=project,
                    from_kind="system", from_id="integration-train", to_kind="session",
                    to_id=incident["supervisor_owner"], subject=summary,
                    body=f"{summary}\n{investigation}\n{decision}\nIncident: {incident_id}",
                    thread_id=incident_id, priority=100, created_at=now, archive_after_inject=1,
                    body_kind="retained_repository_recovery",
                ).on_conflict_do_nothing(index_elements=[messages.c.id]))
            elif incident["investigation"] != investigation:
                # Keep one notification, while its incident carries the latest
                # bounded evidence even if the first notice was already consumed.
                await conn.execute(update(escalations).where(
                    escalations.c.id == incident_id,
                    escalations.c.state.in_(("needs_human", "reply_received", "resolving")),
                ).values(summary=summary, investigation=investigation,
                         revision=escalations.c.revision + 1, updated_at=now))
                await conn.execute(update(messages).where(
                    messages.c.id == "msg-" + incident_id,
                    messages.c.delivered_at.is_(None),
                ).values(subject=summary,
                         body=f"{summary}\n{investigation}\n{decision}\nIncident: {incident_id}"))
    return state
