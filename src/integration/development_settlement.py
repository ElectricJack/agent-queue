"""Deliveries a development target does not owe, and the parks that go stale.

A development repository publishes to one target, ``refs/heads/<default
branch>``.  When an operator retargets it (``aq integration enable --mode
disabled``, ``aq project set … integration-repository`` with a new
``default_branch``, ``aq integration develop``), three things the publisher
knew about the old target must not be carried to the new one:

* **Work already delivered to the old target.**  Its source is not in the new
  branch, so git alone reads it as pending there, and the publisher would merge
  old-branch history into the new branch (or park it and file a repair that
  does).  A generation completed before the retarget whose source the previous
  target contains, or settled on it, is recorded as *not owed* to the new one
  (:data:`DELIVERED_TO_PREVIOUS_TARGET`).
* **Repairs built for the old target.**  A repair starts from the target its
  park named and merges that target's history.  Its delivery is not owed to
  any other target (:data:`REPAIR_FOR_PREVIOUS_TARGET`); its sources are judged
  on the new target on their own.
* **Parks on the old target.**  A park answers "this source conflicts with
  that base".  On another target it holds nothing: the publisher scopes parks
  to its current target and retires the stale rows (``cancelled``, evidence
  ``retired``) so their sources are merged into the new target afresh.

A park on the current target whose members all turn out not to be owed there
is cancelled rather than repaired, and the repairs already filed for it are
settled with it (:data:`SOURCES_NOT_OWED`).

The settlement itself (:data:`~src.integration.delivery_truth.SETTLEMENT_KEY`)
is evaluated by :class:`~src.integration.delivery_truth.DeliverySnapshot`, so
every reader — readiness, claims, publisher, settlement, archive, status —
agrees.  ``aq integration settle-parked`` records the same answer for one
parked row on an operator's or supervisor's reason
(:data:`OPERATOR_SETTLED`).  The supervisor is told about every automatic
settlement: nothing is decided silently.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import messages, task_metadata
from src.integration.delivery_truth import SETTLEMENT_KEY

#: Settlement reasons (``settled: <reason>`` in delivery evidence).
DELIVERED_TO_PREVIOUS_TARGET = "delivered_to_previous_target"
REPAIR_FOR_PREVIOUS_TARGET = "repair_for_previous_target"
#: A repair filed for a park whose members the target turned out not to owe.
SOURCES_NOT_OWED = "repair_sources_not_owed"
OPERATOR_SETTLED = "operator_settled"

#: ``messages.body_kind`` of the supervisor notice about automatic settlements.
SETTLEMENT_MESSAGE_KIND = "development_delivery_settlement"

_REPAIR_TARGET = re.compile(r"^Publication target: (refs/heads/\S+?)\.$", re.MULTILINE)


def publication_target(ref) -> bool:
    """Whether a journal row's *ref* is a publication target.

    Candidate preservation and legacy parent assembly write under
    ``refs/heads/aq/``; only a default branch is delivered to.
    """
    return (
        isinstance(ref, str) and ref.startswith("refs/heads/")
        and not ref.startswith("refs/heads/aq/")
    )


#: Journal rows the publisher itself writes onto its target.  Operator
#: adoption names a ref of its own choosing, so it never says what the target
#: was; nor does a settlement or candidate preservation.
_PUBLISHER_REASONS = frozenset({
    "development batch",
    "source conflict; independent work may continue",
    "selected validation failed",
})


def _configurations(history, repository_id):
    return sorted(
        (
            row for row in history
            if row.get("repository_id") == repository_id
            and (row.get("evidence") or {}).get("kind") == "configuration"
            and publication_target(row.get("target_ref"))
        ),
        key=lambda row: (row.get("created_at") or 0, str(row.get("id"))),
    )


def _published_before(history, repository_id, created_at):
    """The target of the newest publisher row older than *created_at*, if any."""
    rows = [
        row for row in history
        if row.get("repository_id") == repository_id
        and row.get("reason") in _PUBLISHER_REASONS
        and publication_target(row.get("target_ref"))
        and (created_at is None or (row.get("created_at") or 0) < created_at)
    ]
    if not rows:
        return None
    return max(rows, key=lambda row: (row.get("created_at") or 0, str(row.get("id"))))[
        "target_ref"
    ]


def previous_target(history, repository_id, target_ref):
    """The target ``aq integration develop`` is moving away from, or ``None``.

    The newest configuration names it; a journal older than configuration
    rows is read from the publisher's own batch and park rows.
    """
    configurations = _configurations(history, repository_id)
    previous = (
        configurations[-1]["target_ref"] if configurations
        else _published_before(history, repository_id, None)
    )
    return previous if previous and previous != target_ref else None


def retarget_of(history, repository_id, target_ref):
    """The retarget onto *target_ref*, or ``None`` when there was none.

    Returns ``{"from_ref", "at", "operation_id"}``.  Only configuration rows
    move the target: the first of the newest run of configurations on
    *target_ref* is the retarget (``at`` fences the generations that could have
    been delivered before it), and it records the target it replaced; older
    journals name it by the configuration, or failing that the publisher row,
    before it.  Adoptions and settlements written later never move it.
    """
    configurations = _configurations(history, repository_id)
    first = None
    for row in reversed(configurations):
        if row["target_ref"] != target_ref:
            break
        first = row
    if first is None:
        return None
    recorded = ((first.get("evidence") or {}).get("retarget") or {}).get("from_ref")
    index = configurations.index(first)
    if publication_target(recorded):
        from_ref = recorded
    elif index:
        from_ref = configurations[index - 1]["target_ref"]
    else:
        from_ref = _published_before(history, repository_id, first.get("created_at"))
    if not from_ref or from_ref == target_ref:
        return None
    return {
        "from_ref": from_ref,
        "at": float(first.get("created_at") or 0),
        "operation_id": first["id"],
    }


def repair_target_of(description, evidence, history):
    """The target a development repair was filed to publish to, if known.

    Newer repairs record it in their evidence; older ones name the parked row
    it came from, and every repair's description states it.
    """
    if isinstance(evidence, dict):
        if publication_target(evidence.get("target_ref")):
            return evidence["target_ref"]
        delivery_id = evidence.get("delivery_id")
        row = next((row for row in history if row.get("id") == delivery_id), None)
        if row is not None and publication_target(row.get("target_ref")):
            return row["target_ref"]
    match = _REPAIR_TARGET.search(description or "")
    return match.group(1) if match else None


def settlement_record(*, target_ref, repository_id, reason, completion_id, source_oid,
                      detail, authority, operator_id=None, evidence=None, now=None):
    """One :data:`SETTLEMENT_KEY` value: which target, which generation, and why."""
    return {
        "target_ref": target_ref,
        "repository_id": repository_id,
        "reason": reason,
        "completion_id": completion_id,
        "source_oid": source_oid,
        "detail": detail,
        "authority": authority,
        "operator_id": operator_id,
        "evidence": evidence or {},
        "at": time.time() if now is None else now,
    }


async def write_settlements_on(conn, records):
    """Record *records* (task id -> settlement) on the caller's transaction."""
    if not records:
        return
    statement = pg_insert(task_metadata).values([
        {"task_id": task_id, "key": SETTLEMENT_KEY, "value": json.dumps(record)}
        for task_id, record in sorted(records.items())
    ])
    await conn.execute(statement.on_conflict_do_update(
        index_elements=["task_id", "key"], set_={"value": statement.excluded.value},
    ))


_REASON_TEXT = {
    DELIVERED_TO_PREVIOUS_TARGET: "already delivered to the previous target {previous}",
    REPAIR_FOR_PREVIOUS_TARGET: "a repair built for {built_for}, not for this target",
    SOURCES_NOT_OWED: "a repair of work this target does not owe",
}


async def notify_settlements_on(conn, project_id, operation_id, target_ref, records, *,
                                now=None):
    """Tell ``supervisor-<project>`` what was settled automatically, once."""
    lines = []
    for task_id, record in sorted(records.items()):
        evidence = record.get("evidence") or {}
        text = _REASON_TEXT.get(record["reason"], record["reason"]).format(
            previous=evidence.get("previous_target_ref"), built_for=evidence.get("built_for"),
        )
        lines.append(
            f"- {task_id}: {text} (completion {record.get('completion_id') or 'any'}, "
            f"source {record.get('source_oid') or 'unknown'})"
        )
    body = (
        f"The development publisher for {project_id} recorded {len(records)} task(s) as "
        f"not owed to {target_ref} and will not merge them there; their dependents are "
        "released.\n" + "\n".join(lines) + "\n"
        "If any of this work does belong on this target, merge its source there on a "
        "branch that keeps it as an ancestor (then `aq integration adopt`), or reopen and "
        "close the task again: a new completion is owed again. A repair listed here that "
        "is still being worked on is no longer needed; retire it with "
        "`aq task close <id> --obsolete --reason ...` once its session has stopped. "
        f"Journal: `aq integration status {project_id}` (row {operation_id})."
    )
    now = time.time() if now is None else now
    await conn.execute(pg_insert(messages).values(
        id="development-settlement-" + hashlib.sha256(operation_id.encode()).hexdigest()[:24],
        project_id=project_id,
        from_kind="system",
        from_id="development-integration",
        to_kind="session",
        to_id=f"supervisor-{project_id}",
        subject=(
            f"Development delivery: {len(records)} task(s) not owed to {target_ref} "
            f"in {project_id}"
        ),
        body=body,
        created_at=now,
        priority=50,
        archive_after_inject=1,
        body_kind=SETTLEMENT_MESSAGE_KIND,
    ).on_conflict_do_nothing(index_elements=[messages.c.id]))
