"""Retire the development_deliveries receipt journal; git answers delivery.

Revision ID: a00000000038
Revises: a00000000037

Nothing reads a delivery row for eligibility any more: every consumer asks
git (``src/integration/delivery_truth.py``) and the publisher appends its
genuine actions to ``development.operation`` events.  Before dropping the
table this revision keeps what is not a delivery answer, and only that:

* an **outstanding action** -- a ``prepared``/``publishing``/``parked`` row, a
  validation deferral or a pending branch cleanup -- becomes the
  ``development.operation`` event ``legacy-operation:<id>``.  Identity, dedupe
  and dropped keys match ``aq integration migrate-provenance``'s retention, so
  running either first, or this revision twice, retains each action once;
* the **source provenance** of every row that names one becomes one immutable
  ``development.legacy_provenance`` event: its identity, target, member
  sources, exact ``completion_sources`` bindings, repair replacement mapping,
  and the test/conflict evidence it recorded.  ``state`` and the receipt
  conclusions (``resolved_by_main_ancestry``, branch cleanup, diagnostics) are
  not kept.  Only the operator migration (which writes git provenance) and a
  legacy repair's filing fence read these events;
* a live COMPLETED **branchless task** a manifest names with a source its
  current completion never recorded is marked ``development_legacy_artifact``
  (fenced to that completion).  Without the manifest it would read as an
  organizational container; the marker keeps its delivery *unknown* until an
  operator retains an exact generation, instead of fabricating "no artifact".

What could not be resolved here is reported, not guessed: one
``development.legacy_retirement`` event per project lists the retained actions,
the marked tasks, archived tasks of the same shape and malformed rows, and the
same counts are logged.  ``aq integration migrate-provenance`` then binds or
reports every remaining generation against git.  Downgrade recreates an empty
journal; the retained events and markers stay (they are history).
"""

from __future__ import annotations

import json
import logging
import re
import time

import sqlalchemy as sa
from alembic import op

revision = "a00000000038"
down_revision = "a00000000037"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "development_deliveries"
OPERATION_EVENT = "development.operation"
PROVENANCE_EVENT = "development.legacy_provenance"
SUMMARY_EVENT = "development.legacy_retirement"
ARTIFACT_KEY = "development_legacy_artifact"
OPEN_STATES = {"prepared", "publishing", "parked"}
DEFERRAL_KIND = "validation_deferred"
CLEANUP_KEY = "branch_cleanup"
# Receipt bindings are git's to answer; an operation never carries them.
BINDING_KEYS = {"completion_sources", "resolved_by_delivered_repair", "resolved_by_main_ancestry"}
MEMBER_KEYS = ("task_id", "source_sha", "parent_task_id", "superseded_by", "acceptance")
TEST_KEYS = ("validation", "conclusion", "checks", "failing_tests", "head_sha")
CONFLICT_KEYS = (
    "detail", "conflicting_files", "conflict_with", "conflicting_members",
    "target_sha", "aggregate_sha", "source_sha",
)
CHUNK = 500
_OID = re.compile(r"[0-9a-f]{40}")


def _rows(bind):
    """Every journal row in keyset chunks, JSON decoded from text."""
    after = None
    while True:
        where, params = "", {"limit": CHUNK}
        if after is not None:
            where, params = "WHERE (created_at, id) > (:at, :id)", {
                **params, "at": after[0], "id": after[1],
            }
        chunk = bind.execute(sa.text(
            "SELECT id, project_id, repository_id, target_ref, expected_sha, prepared_sha, "
            "state, manifest::text AS manifest, evidence::text AS evidence, reason, "
            f"created_at, updated_at FROM {TABLE} {where} "
            "ORDER BY created_at, id LIMIT :limit"
        ), params).mappings().all()
        for row in chunk:
            row = dict(row)
            row["manifest"] = json.loads(row["manifest"])
            row["evidence"] = json.loads(row["evidence"])
            yield row
        if len(chunk) < CHUNK:
            return
        after = chunk[-1]["created_at"], chunk[-1]["id"]


def _members(manifest):
    return [m for m in manifest if isinstance(m, dict) and m.get("task_id")] if isinstance(
        manifest, list
    ) else []


def _outstanding(row, evidence):
    cleanup = evidence.get(CLEANUP_KEY) or {}
    return (row["state"] in OPEN_STATES or evidence.get("kind") == DEFERRAL_KIND
            or (isinstance(cleanup, dict) and cleanup.get("state") == "pending"))


def _operation(row, evidence):
    """The retained action, shaped as ``DevelopmentIntegration._operation_insert`` writes."""
    state = "finished" if row["state"] in {"delivered", "adopted"} else row["state"]
    return {
        "expected_sha": row["expected_sha"], "prepared_sha": row["prepared_sha"],
        "project_id": row["project_id"], "repository_id": row["repository_id"],
        "target_ref": row["target_ref"], "manifest": row["manifest"], "reason": row["reason"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "id": "legacy-operation:" + row["id"], "state": state,
        "evidence": {k: v for k, v in evidence.items() if k not in BINDING_KEYS},
    }


def _provenance(row, evidence, members):
    """Source locators and recorded test/conflict evidence; never a delivery state."""
    retained = {
        "id": "legacy-provenance:" + row["id"], "legacy_id": row["id"],
        "project_id": row["project_id"], "repository_id": row["repository_id"],
        "target_ref": row["target_ref"], "expected_sha": row["expected_sha"],
        "prepared_sha": row["prepared_sha"], "created_at": row["created_at"],
        "kind": evidence.get("kind"),
        "manifest": [{k: m[k] for k in MEMBER_KEYS if k in m} for m in members],
    }
    for key in ("completion_sources", "resolved_by_delivered_repair"):
        if evidence.get(key):
            retained[key] = evidence[key]
    tests = {k: evidence[k] for k in TEST_KEYS if k in evidence}
    if "checks" in tests or "conclusion" in tests:
        retained["tests"] = tests
    if evidence.get("kind") == "merge_conflict":
        retained["conflict"] = {k: evidence[k] for k in CONFLICT_KEYS if k in evidence}
    return retained


def _latest_completions(bind, task_ids):
    rows = bind.execute(sa.text(
        "SELECT DISTINCT ON (task_id) task_id, id, commits, completed_at "
        "FROM task_completion_records WHERE task_id = ANY(:ids) "
        "ORDER BY task_id, completed_at DESC, id DESC"
    ), {"ids": sorted(task_ids)}).mappings().all()
    return {row["task_id"]: row for row in rows}


def _unrecorded(completion):
    if completion is None:
        return True
    try:
        commits = json.loads(completion["commits"] or "[]")
    except ValueError:
        return False  # malformed: the scope already treats it as recorded
    return not commits


def _artifacts(bind, named):
    """Branchless tasks whose current completion lacks a source a manifest names.

    *named* maps task id -> [(row, source, bound completion id or None)]. A
    manifest older than the current completion located an earlier generation
    and is ignored, unless its ``completion_sources`` bound this exact one.
    Returns ``(live markers, archived task ids)``.
    """
    if not named:
        return {}, []
    ids = sorted(named)
    live = {row["id"]: row for row in bind.execute(sa.text(
        "SELECT id, status, branch_name FROM tasks WHERE id = ANY(:ids)"
    ), {"ids": ids}).mappings()}
    archived = {row["id"]: row for row in bind.execute(sa.text(
        "SELECT id, status, branch_name FROM archived_tasks WHERE id = ANY(:ids)"
    ), {"ids": ids}).mappings()}
    completions = _latest_completions(bind, ids)
    markers, archived_ids = {}, []
    for task_id in ids:
        task = live.get(task_id) or archived.get(task_id)
        if task is None or task["branch_name"] is not None or task["status"] != "COMPLETED":
            continue
        completion = completions.get(task_id)
        if not _unrecorded(completion):
            continue
        generation = completion["id"] if completion else None
        located = [
            (row, source) for row, source, bound in named[task_id]
            if (bound is not None and bound == generation) or (
                bound is None
                and (completion is None or row["created_at"] >= completion["completed_at"])
            )
        ]
        if not located:
            continue
        if task_id not in live:
            archived_ids.append(task_id)
            continue
        row, source = max(located, key=lambda item: (item[0]["created_at"], item[0]["id"]))
        markers[task_id] = {
            "legacy_id": row["id"], "source_sha": source, "completion_id": generation,
            "project_id": row["project_id"],
            "reason": "a retired development_deliveries manifest names a source this "
                      "generation never recorded; delivery is unknown until an exact "
                      "completion generation is retained in git",
        }
    return markers, archived_ids


def _insert_event(bind, event_type, project_id, payload, now):
    bind.execute(sa.text(
        "INSERT INTO events (event_type, project_id, payload, timestamp) "
        "VALUES (:type, :project, :payload, :at)"
    ), {"type": event_type, "project": project_id, "payload": json.dumps(payload), "at": now})


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    now = time.time()
    present = set(bind.execute(sa.text(
        "SELECT DISTINCT payload::jsonb->>'id' FROM events WHERE event_type = ANY(:types)"
    ), {"types": [OPERATION_EVENT, PROVENANCE_EVENT, SUMMARY_EVENT]}).scalars())
    summaries: dict[str, dict] = {}
    named: dict[str, list] = {}
    for row in _rows(bind):
        summary = summaries.setdefault(row["project_id"], {
            "id": "legacy-retirement:" + row["project_id"], "project_id": row["project_id"],
            "rows": 0, "operations_retained": [], "provenance_retained": 0,
            "unresolved_artifacts": [], "unresolved_archived": [], "malformed": [],
        })
        summary["rows"] += 1
        evidence = row["evidence"] if isinstance(row["evidence"], dict) else {}
        members = _members(row["manifest"])
        if not isinstance(row["manifest"], list) or not isinstance(row["evidence"], dict):
            summary["malformed"].append(row["id"])
        if _outstanding(row, evidence):
            operation = _operation(row, evidence)
            if operation["id"] not in present:
                _insert_event(bind, OPERATION_EVENT, row["project_id"], operation, now)
                present.add(operation["id"])
            summary["operations_retained"].append(row["id"])
        proofs = [p for p in evidence.get("completion_sources") or [] if isinstance(p, dict)]
        if members or proofs or evidence.get("resolved_by_delivered_repair") or (
            "checks" in evidence
        ):
            retained = _provenance(row, evidence, members)
            if retained["id"] not in present:
                _insert_event(bind, PROVENANCE_EVENT, row["project_id"], retained, now)
                present.add(retained["id"])
            summary["provenance_retained"] += 1
        for member in members:
            source = member.get("source_sha")
            if isinstance(source, str) and _OID.fullmatch(source):
                named.setdefault(member["task_id"], []).append((row, source, None))
        for proof in proofs:
            source = proof.get("source_sha")
            if proof.get("task_id") and isinstance(source, str) and _OID.fullmatch(source):
                named.setdefault(proof["task_id"], []).append(
                    (row, source, proof.get("completion_id"))
                )

    markers, archived_ids = _artifacts(bind, named)
    for task_id, marker in sorted(markers.items()):
        bind.execute(sa.text(
            "INSERT INTO task_metadata (task_id, key, value) VALUES (:task, :key, :value) "
            "ON CONFLICT (task_id, key) DO NOTHING"
        ), {"task": task_id, "key": ARTIFACT_KEY, "value": json.dumps(marker)})
        summaries[marker["project_id"]]["unresolved_artifacts"].append(task_id)
    for task_id in archived_ids:
        project_id = next(row["project_id"] for row, _source, _bound in named[task_id])
        summaries[project_id]["unresolved_archived"].append(task_id)

    for project_id, summary in sorted(summaries.items()):
        if summary["id"] not in present:
            _insert_event(bind, SUMMARY_EVENT, project_id, summary, now)
        logger.warning(
            "retired %d development_deliveries rows of %s: %d outstanding actions retained, "
            "%d provenance records retained; delivery unknown until migrated for %s; "
            "archived of the same shape: %s; malformed rows: %s. Run `aq integration "
            "migrate-provenance %s` to bind or report every remaining generation.",
            summary["rows"], project_id, len(summary["operations_retained"]),
            summary["provenance_retained"], summary["unresolved_artifacts"] or "none",
            summary["unresolved_archived"] or "none", summary["malformed"] or "none", project_id,
        )
    op.drop_table(TABLE)


def downgrade() -> None:
    """Recreate an empty journal; retained events and markers remain history."""
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        return
    from migrations.versions.a0000000000c_development_integration import (
        development_deliveries_table,
    )

    development_deliveries_table().create(bind, checkfirst=True)
