"""Lock database-backed source rows before the dispatcher's fresh observation.

External watcher and resource readers own their typed currentness/locking
contract; composition must supply those callbacks, never treat their event as
an admission or remote-state verdict.
"""

from sqlalchemy import select

from src.database import tables as t
from src.waits.listener import ListenerUnavailable


async def lock_database_source(conn, cond):
    p = cond.parameters
    subjects = []
    if cond.kind == "timer":
        return  # The bound UTC instant is immutable; the adapter reads the clock.
    if cond.kind == "task":
        subjects = [(t.tasks, t.tasks.c.id == p["task_id"])]
    elif cond.kind == "dependency":
        subjects = [(t.tasks, t.tasks.c.id.in_(sorted([p["task_id"], p["depends_on_task_id"]]))),
                    (t.task_dependencies, (t.task_dependencies.c.task_id == p["task_id"])
                     & (t.task_dependencies.c.depends_on_task_id == p["depends_on_task_id"])
                     & (t.task_dependencies.c.dep_type == p["dep_type"]))]
    elif cond.kind == "job":
        subjects = [(t.jobs, t.jobs.c.id == p["job_id"])]
    elif cond.kind == "batch":
        subjects = [(t.integration_batches, t.integration_batches.c.id == p["batch_id"]),
                    (t.integration_batch_members,
                     t.integration_batch_members.c.batch_id == p["batch_id"])]
    elif cond.kind == "playbook_condition":
        subjects = [(t.playbook_v2_runs, t.playbook_v2_runs.c.run_id == p["run_id"]),
                    (t.playbook_waits, t.playbook_waits.c.wait_id == p["wait_id"])]
    elif cond.kind == "human" and p["subject"] == "legacy_escalation":
        subjects = [(t.escalations, t.escalations.c.id == p['escalation_id'])]
    elif cond.kind == "human" and p["subject"] == "review":
        subjects = [(t.doc_reviews, t.doc_reviews.c.id == p["review_id"])]
    elif cond.kind == "human" and p["subject"] == "task_comment":
        subjects = [(t.tasks, t.tasks.c.id == p["task_id"]),
                    (t.task_comments, t.task_comments.c.task_id == p["task_id"])]
    elif cond.kind in {"message", "human"} and "thread_id" in p:
        subjects = [(t.collaboration_threads, t.collaboration_threads.c.id == p["thread_id"]),
                    (t.collaboration_members,
                     t.collaboration_members.c.thread_id == p["thread_id"]),
                    (t.messages, t.messages.c.thread_id == p["thread_id"])]
    else:
        raise ListenerUnavailable(f"{cond.kind} requires its source owner's lock callback")
    for table, predicate in subjects:
        await conn.execute(select(table).where(predicate).order_by(
            *table.primary_key.columns,
        ).with_for_update(read=True))
