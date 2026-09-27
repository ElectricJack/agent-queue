"""Bounded observation of development publisher skips.

The publisher skips a completed candidate it cannot publish yet: an
undelivered dependency, a missing source ref, a dependency cycle, a parked
source.  A skip that repeats forever used to be indistinguishable from
progress — one candidate reached 35 consecutive skips over several hours while
doctor called the publisher healthy.  This module gives a skip a bounded life.

Each evaluation of a skipped candidate is *fingerprinted*: task, latest
completion generation, reason, the related task, the configured target
(repository and ref) and the relevant git evidence — the candidate's observed
source ref OID and, for reasons decided by merging into the target, the
target OID.  For every other reason the sweep re-proves non-containment
against the current target on each evaluation, so a target that advanced
without containing the work changes nothing relevant; counting it as new
evidence would let any unrelated delivery reset a genuine stall.

A run of identical fingerprints is one *attempt*.  After
``integration.publisher_stall_after`` consecutive identical unsuccessful
evaluations (default five) the attempt ends as ``stalled``: the publisher logs
one error, queues one supervisor message and doctor reports ERROR.  Later
identical evaluations neither count further nor notify again.  The sweep keeps
evaluating a stalled candidate, because fresh git truth — its source reaching
the target, a dependency delivered — is still what releases it.  Changed
evidence, or an explicit ``aq integration sweep --retry`` / ``--recover-child``,
starts a new attempt.

A skip that waits on a live repair task is not an unsuccessful evaluation: the
repair is delegated work with its own lifecycle, so the record says what it is
waiting on and does not count.  When the repair fails or finishes without
releasing the source, the wait ends and counting starts.

Only observations and notification deduplication are persisted, in the
``development_publisher_skip`` task metadata row.  Nothing here is delivery
truth: no reader may treat a record as proof that work did or did not reach
the target.  Records survive a daemon restart, so neither the count nor the
dedupe resets; idle ticks evaluate nothing and downtime is not counted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    archived_tasks,
    messages,
    task_completion_records,
    task_metadata,
    tasks,
)
from src.models import TaskStatus

#: The publisher's logger: these lines belong to its sweep.
logger = logging.getLogger("src.integration.development")

#: Task metadata key holding a candidate's latest skip observation.
PUBLISHER_SKIP_KEY = "development_publisher_skip"
#: Consecutive identical unsuccessful evaluations that end an attempt.
DEFAULT_STALL_AFTER = 5

OBSERVING = "observing"
WAITING = "waiting"
STALLED = "stalled"

#: ``messages.body_kind`` of the supervisor notification.
STALL_MESSAGE_KIND = "development_publisher_stall"

#: Reasons whose outcome is a merge against the current target, so a moved
#: target is new evidence.
TARGET_SENSITIVE_REASONS = frozenset({"merge_conflict", "parent_unavailable"})

#: Reasons a live repair can resolve, and whose task that repair would free:
#: the candidate itself, or the dependency it waits for.
_REPAIR_WAITS = {"source_parked": "self", "undelivered_dependency": "related"}

#: A repair still working on a parked source.  A completed repair is live only
#: while it is itself pending publication (a current candidate).
_FINISHED_REPAIR_STATUSES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}
)

#: One line of recovery advice per reason, quoted in the supervisor message.
_RECOVERY = {
    "missing_ref": (
        "its source ref is gone and its completion's source is not on the target: push "
        "the branch again, or close the task again with the commit that holds the work"
    ),
    "undelivered_dependency": (
        "a dependency is not delivered: publish or recover that dependency first"
    ),
    "dependency_cycle": (
        "its dependency edges form a cycle with no proven replacement: remove the "
        "wrong edge or publish a repair that carries both sources"
    ),
    "dependency_cycle_source_changed": (
        "a source listed by the replacing repair moved: re-run the repair on the "
        "current source"
    ),
    "source_parked": (
        "its source is parked and no live repair is working on it: read the parked "
        "batch in `aq integration status`, then repair, adopt or cancel it"
    ),
    "merge_conflict": "its source conflicts with the target: rebase it in a repair",
    "parent_unavailable": "a sibling conflicted in parent assembly",
}


@dataclass(frozen=True)
class SweepObservation:
    """What one sweep saw, for fingerprinting the candidates it skipped."""

    project_id: str
    repository_id: str
    target_ref: str
    target_sha: str
    #: Candidate id -> its recorded branch name.
    branches: dict[str, str]
    #: ``refs/remotes/origin/<branch>`` -> OID, pinned by the sweep.
    source_heads: dict[str, str]
    #: The publisher's journal rows (parked rows name the sources a repair holds).
    history: list[dict] = field(default_factory=list)
    #: Every candidate still pending publication in this sweep.
    pending: frozenset[str] = frozenset()

    def source_of(self, task_id):
        branch = self.branches.get(task_id)
        if not branch:
            return None
        return self.source_heads.get(
            "refs/remotes/origin/" + branch.removeprefix("refs/heads/")
        )


def fingerprint(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, default=str).encode()
    ).hexdigest()[:24]


def _attempt_id(task_id, digest, started_at):
    return hashlib.sha256(f"{task_id}|{digest}|{started_at!r}".encode()).hexdigest()[:16]


def stall_message_id(project_id, attempt_ids):
    """One message per set of attempts that stalled together; restart-safe."""
    material = project_id + "|" + ",".join(sorted(attempt_ids))
    return "msg-dev-publisher-stall-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def is_stalled(record, *, stall_after=DEFAULT_STALL_AFTER):
    """Whether *record* describes a stalled attempt.

    A record written before attempts had states carries only a count; it is
    judged by the threshold until the next sweep rewrites it.
    """
    if not isinstance(record, dict):
        return False
    state = record.get("state")
    if state is not None:
        return state == STALLED
    return (record.get("consecutive_ticks") or 0) >= stall_after


class PublisherStalls:
    """Record skip observations and end identical runs of them as stalls."""

    def __init__(self, db, *, stall_after=DEFAULT_STALL_AFTER,
                 repair_identity: Callable[[list], str]):
        if isinstance(stall_after, bool) or not isinstance(stall_after, int) or stall_after < 1:
            raise ValueError("publisher stall threshold must be a positive integer")
        self.db = db
        self.stall_after = stall_after
        self.repair_identity = repair_identity

    async def clear(self, project_id):
        """Nothing is left to evaluate, so no skip record is current."""
        await self._write(project_id, keep=frozenset(), cleared=(), records={}, message=None)

    async def observe(self, observation: SweepObservation, processed, skipped, *,
                      keep=None, fresh=()):
        """Persist this sweep's skip observations; return attempts that just stalled.

        *processed* are the candidates the sweep evaluated and *skipped* maps
        the unpublishable ones to ``(reason, related_task_id)``.  A processed
        candidate that was not skipped loses its record.  With *keep*, records
        of tasks outside it — delivered, adopted, obsolete, reopened or
        archived, so no longer candidates — are dropped too.  Tasks in
        *fresh* were named by an explicit recovery and start a new attempt.
        """
        now = time.time()
        ids = sorted(processed)
        previous = await self._records(ids)
        generations = await self._generations(sorted(skipped))
        waits = await self._live_repairs(observation, skipped)
        fresh = set(fresh)
        records, cleared, crossed = {}, [], []
        for task_id in ids:
            old = previous.get(task_id)
            if task_id not in skipped:
                if old is not None:
                    cleared.append(task_id)
                    if is_stalled(old, stall_after=self.stall_after):
                        logger.info(
                            "development publisher: %s is no longer skipped; its stall "
                            "(%s) is cleared", task_id, old.get("reason"),
                            extra={"candidate_task_id": task_id,
                                   "project": observation.project_id},
                        )
                continue
            kind, related = skipped[task_id]
            record = self.advance(
                task_id, old, kind, related, observation,
                completion_id=generations.get(task_id), waiting_on=waits.get(task_id),
                fresh=task_id in fresh, now=now,
            )
            records[task_id] = record
            if record["state"] == STALLED and not record.get("notified_message_id"):
                crossed.append(task_id)
        message = None
        if crossed:
            message_id = stall_message_id(
                observation.project_id, [records[t]["attempt_id"] for t in crossed]
            )
            for task_id in crossed:
                records[task_id]["notified_message_id"] = message_id
                logger.error(
                    "development publisher: %s stalled after %d identical evaluations: %s%s",
                    task_id, records[task_id]["consecutive_ticks"], records[task_id]["reason"],
                    f" ({records[task_id]['dependency_id']})"
                    if records[task_id].get("dependency_id") else "",
                    extra={"candidate_task_id": task_id, "project": observation.project_id,
                           "reason": records[task_id]["reason"]},
                )
            message = self._message(observation, message_id,
                                    {t: records[t] for t in crossed}, now=now)
        await self._write(observation.project_id, keep=keep, cleared=cleared,
                          records=records, message=message)
        return [{"task_id": t, **records[t]} for t in crossed]

    def advance(self, task_id, old, kind, related, observation, *, completion_id,
                waiting_on, fresh, now):
        """The record after one more skip of *task_id*, given its *old* record."""
        evidence = {
            "repository_id": observation.repository_id,
            "target_ref": observation.target_ref,
            "target_sha": observation.target_sha,
            "source_sha": observation.source_of(task_id),
            "completion_id": completion_id,
        }
        if waiting_on:
            evidence["waiting_on"] = waiting_on
        identity = {
            "task_id": task_id, "reason": kind, "dependency_id": related,
            **{k: v for k, v in evidence.items() if k != "target_sha"},
        }
        if kind in TARGET_SENSITIVE_REASONS:
            identity["target_sha"] = observation.target_sha
        digest = fingerprint(identity)
        old = old if isinstance(old, dict) else None
        same = not fresh and old is not None and (
            old.get("fingerprint") == digest if "fingerprint" in old else (
                # Written before fingerprints existed: continue its count.
                old.get("reason") == kind and old.get("dependency_id") == related
                and not waiting_on
            )
        )
        if same:
            started = old.get("first_skipped_at") or now
            count = old.get("consecutive_ticks", 0) or 0
            state = old.get("state") or OBSERVING
            attempt = old.get("attempt_id") or _attempt_id(task_id, digest, started)
        else:
            started, count, state = now, 0, OBSERVING
            attempt = _attempt_id(task_id, digest, started)
        record = {
            "reason": kind,
            "dependency_id": related,
            "fingerprint": digest,
            "attempt_id": attempt,
            "evidence": evidence,
            "first_skipped_at": started,
            "last_skipped_at": now,
        }
        if waiting_on:
            # Delegated work, not a failed evaluation: nothing counts.
            return {**record, "state": WAITING, "waiting_on": waiting_on,
                    "consecutive_ticks": 0}
        if state == STALLED:
            # The attempt is over; a fresh check that finds the same thing is
            # neither counted nor reported again.
            return {**record, "state": STALLED, "consecutive_ticks": count,
                    "stall_after": old.get("stall_after", self.stall_after),
                    "stalled_at": old.get("stalled_at", now),
                    "notified_message_id": old.get("notified_message_id")}
        count += 1
        if count >= self.stall_after:
            return {**record, "state": STALLED, "consecutive_ticks": count,
                    "stall_after": self.stall_after, "stalled_at": now}
        return {**record, "state": OBSERVING, "consecutive_ticks": count}

    async def _records(self, task_ids):
        if not task_ids:
            return {}
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(
                select(task_metadata.c.task_id, task_metadata.c.value).where(
                    task_metadata.c.task_id.in_(task_ids),
                    task_metadata.c.key == PUBLISHER_SKIP_KEY,
                )
            )).all()
        records = {}
        for task_id, raw in rows:
            try:
                records[task_id] = json.loads(raw)
            except (TypeError, ValueError):
                records[task_id] = None
        return records

    async def _generations(self, task_ids):
        """Latest completion record id per task: its completion generation."""
        if not task_ids:
            return {}
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(
                select(task_completion_records.c.task_id, task_completion_records.c.id)
                .where(task_completion_records.c.task_id.in_(task_ids))
                .order_by(task_completion_records.c.completed_at,
                          task_completion_records.c.id)
            )).all()
        return {task_id: completion_id for task_id, completion_id in rows}

    async def _live_repairs(self, observation, skipped):
        """Map each skipped candidate waiting on a live repair to that repair."""
        blocking = {}
        for task_id, (kind, related) in skipped.items():
            whose = _REPAIR_WAITS.get(kind)
            holder = task_id if whose == "self" else related if whose else None
            if holder:
                blocking[task_id] = holder
        if not blocking:
            return {}
        repairs = {}
        for row in observation.history:
            if (row.get("state") != "parked"
                    or row.get("repository_id") != observation.repository_id):
                continue
            members = [m for m in row.get("manifest") or []
                       if isinstance(m, dict) and m.get("task_id")]
            if not members:
                continue
            identity = self.repair_identity(row["manifest"])
            for member in members:
                repairs.setdefault(member["task_id"], []).append(identity)
        candidates = {r for holder in blocking.values() for r in repairs.get(holder, [])}
        if not candidates:
            return {}
        async with self.db._engine.connect() as conn:
            statuses = dict((await conn.execute(
                select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(candidates))
            )).all())
            missing = candidates - statuses.keys()
            if missing:
                statuses.update((await conn.execute(
                    select(archived_tasks.c.id, archived_tasks.c.status)
                    .where(archived_tasks.c.id.in_(missing))
                )).all())

        def live(repair_id):
            status = statuses.get(repair_id)
            if status is None:
                return False
            if status not in _FINISHED_REPAIR_STATUSES:
                return True
            return status == TaskStatus.COMPLETED.value and repair_id in observation.pending

        waits = {}
        for task_id, holder in blocking.items():
            found = next((r for r in sorted(repairs.get(holder, [])) if live(r)), None)
            if found:
                waits[task_id] = found
        return waits

    def _message(self, observation, message_id, stalled, *, now):
        project_id = observation.project_id
        lines = []
        for task_id, record in sorted(stalled.items()):
            evidence = record["evidence"]
            related = record.get("dependency_id")
            source = evidence.get("source_sha") or "missing"
            lines.append(
                f"- {task_id}: {record['reason']}"
                + (f" ({related})" if related and related != task_id else "")
                + f" — {record['consecutive_ticks']} identical evaluations since "
                + time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(record["first_skipped_at"]))
                + f". Repository {evidence['repository_id']}, target "
                + f"{evidence['target_ref']} @ {evidence['target_sha']}, source "
                + f"{observation.branches.get(task_id) or '(none)'} @ {source}, completion "
                + f"{evidence.get('completion_id') or '(none)'}. "
                + _RECOVERY.get(record["reason"], "read the skip record")
                + f". Retry: `aq integration sweep {project_id} --recover-child {task_id}`."
            )
        body = (
            f"The development publisher for {project_id} stopped retrying "
            f"{len(stalled)} candidate(s) after {self.stall_after} identical unsuccessful "
            "evaluations. It still checks each one on every sweep and releases it "
            "as soon as git shows the work delivered; changed source, completion or "
            "target evidence starts a new attempt.\n"
            + "\n".join(lines)
            + f"\nEvidence: `aq doctor --check integration.development_publisher_stalled`, "
            f"`aq integration status {project_id}`."
        )
        return {
            "id": message_id,
            "project_id": project_id,
            "from_kind": "system",
            "from_id": "development-integration",
            "to_kind": "session",
            "to_id": f"supervisor-{project_id}",
            "subject": (
                f"Development publisher stalled on {len(stalled)} candidate(s) in {project_id}"
            ),
            "body": body,
            "created_at": now,
            "priority": 50,
            "archive_after_inject": 1,
            "body_kind": STALL_MESSAGE_KIND,
        }

    async def _write(self, project_id, *, keep, cleared, records, message):
        """Apply one sweep's observations, and its notification, atomically."""
        async with self.db._engine.begin() as conn:
            if keep is not None:
                stale = [
                    task_id for task_id in (await conn.execute(
                        select(task_metadata.c.task_id)
                        .join(tasks, tasks.c.id == task_metadata.c.task_id)
                        .where(tasks.c.project_id == project_id,
                               task_metadata.c.key == PUBLISHER_SKIP_KEY)
                    )).scalars()
                    if task_id not in keep
                ]
                cleared = [*cleared, *stale]
            if cleared:
                await conn.execute(delete(task_metadata).where(
                    task_metadata.c.task_id.in_(sorted(set(cleared))),
                    task_metadata.c.key == PUBLISHER_SKIP_KEY,
                ))
            if records:
                statement = pg_insert(task_metadata).values([
                    {"task_id": task_id, "key": PUBLISHER_SKIP_KEY, "value": json.dumps(record)}
                    for task_id, record in sorted(records.items())
                ])
                await conn.execute(statement.on_conflict_do_update(
                    index_elements=["task_id", "key"],
                    set_={"value": statement.excluded.value},
                ))
            if message is not None:
                await conn.execute(
                    pg_insert(messages).values(**message)
                    .on_conflict_do_nothing(index_elements=[messages.c.id])
                )
