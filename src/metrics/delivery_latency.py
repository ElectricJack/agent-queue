"""Generation-based delivery latency: approval and completion to first sight on dev.

Telemetry only. ``record`` turns a DeliveryObserver view into transition-only
``delivery.observation`` events; ``snapshot`` aggregates a completion cohort.
Nothing that admits, claims, publishes or cleans up may read these samples:
deleting the events changes the dashboard and nothing else.

Times are daemon wall clock (UTC epoch seconds), never commit dates. The
headline delay runs from approval (the spec decision that filed the work, or
the task's creation when there was none) to the first fresh containment on
the project's dev ref, minus linked human-wait time (revision 4, decision 6).
The spec's completion-to-dev delay is reported beside it. Segments come from
``integration``-origin wait records and their ``wait_measurements``; they may
overlap and are never forced to sum to the total.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

from sqlalchemy import and_, func, or_, select

from src.database.tables import (
    archived_tasks,
    ask_decisions,
    doc_reviews,
    events,
    ingest_batch_members,
    ingest_batches,
    integration_batch_members,
    integration_batches,
    integration_repair_operations as repair_operations,
    projects,
    repos,
    task_completion_records as completions,
    tasks,
    wait_measurements as measurements,
    wait_records as waits,
)

EVENT_TYPE = "delivery.observation"
WINDOWS = {"24h": 86_400.0, "7d": 604_800.0}
#: Section 9.2 defaults: ``max(min_seconds, ci_multiplier * p90 batch CI)``.
TARGET_MIN_SECONDS = 1_800.0
TARGET_CI_MULTIPLIER = 2.0
TARGET_CI_LOOKBACK = 604_800.0
SEGMENTS = ("slot_wait", "ci_time", "train_wait", "repair", "author_merge")
KIND_SEGMENT = {
    "resource": "slot_wait",
    "job": "ci_time",
    "ci_run": "ci_time",
    "ci_check": "ci_time",
    "batch": "train_wait",
    "dependency": "train_wait",
    "pull_request": "author_merge",
    "task": "repair",
}
CI_KINDS = frozenset(kind for kind, segment in KIND_SEGMENT.items() if segment == "ci_time")
CODE_FREE_STATES = frozenset({"no_artifact", "no_change", "not_applicable"})
CODE_FREE_OUTCOMES = frozenset({"no-op", "no_op", "noop"})
EXCLUSIONS = ("delivered_by_other_means", "code_free", "abandoned", "superseded", "settled")
DRILLDOWN_LIMIT = 200
#: Linked wait records shown per generation, live ones first.
WAIT_LIMIT = 12
_ANCESTRY_DEPTH = 32


# ---------------------------------------------------------------- recording


def generation_of(request) -> str | None:
    """One completed generation: its completion id, or the legacy identity."""
    if request.completion_id:
        return request.completion_id
    return f"legacy:{request.legacy_generation}" if request.legacy_generation else None


def observation_rows(view, *, now: float) -> list[tuple[tuple, tuple, dict]]:
    """``(key, mark, event row)`` per fresh, known observation in ``view``.

    The key is (task, generation, target ref) and the mark (state, exclusion);
    a recorder writes a row only when the mark changes. Unknown answers and
    failed fetches carry no observation, so they are never written.
    """
    snapshots = {
        (snapshot.project_id, snapshot.repository_id, snapshot.target_ref): snapshot
        for snapshot in view.snapshots
    }
    found = []
    for task_id, evidence in sorted(view.evidence.items()):
        state = str(evidence.state)
        request = evidence.request
        generation = generation_of(request)
        if state == "unknown" or generation is None:
            continue
        snapshot = snapshots.get((request.project_id, request.repository_id, request.target_ref))
        if snapshot is None or snapshot.error is not None:
            continue
        exclusion = evidence.lifecycle_exclusion
        payload = {
            "generation": generation,
            "completion_id": request.completion_id,
            "completed_at": request.completed_at,
            "repository_id": request.repository_id,
            "target_ref": request.target_ref,
            "state": state,
            "reason": evidence.reason,
            "exclusion": exclusion,
            "target_oid": evidence.target_oid,
            "source_oid": evidence.source_oid,
            "observed_at": snapshot.observed_at,
            "collector_at": now,
        }
        found.append((
            (task_id, generation, request.target_ref), (state, exclusion),
            {"event_type": EVENT_TYPE, "project_id": request.project_id, "task_id": task_id,
             "payload": json.dumps(payload, sort_keys=True)},
        ))
    return found


def observation_key(task_id: str, payload: dict) -> tuple[tuple, tuple]:
    return ((task_id, payload.get("generation"), payload.get("target_ref")),
            (payload.get("state"), payload.get("exclusion")))


# -------------------------------------------------------------- arithmetic


def quantile(values, q: float) -> float | None:
    """Linear-interpolated quantile; None without data, never zero."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def union_seconds(intervals) -> float:
    """Seconds covered by the union of ``(start, end)`` intervals."""
    total, current_start, current_end = 0.0, None, None
    for start, end in sorted(interval for interval in intervals if interval[1] > interval[0]):
        if current_end is None or start > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += current_end - current_start
    return total


def _clip(intervals, start, end):
    return [(max(a, start), min(b, end)) for a, b in intervals if min(b, end) > max(a, start)]


def delivery_target(ci_seconds) -> dict:
    """Section 9.2's derived threshold, which is also the SLO target."""
    p90 = quantile(ci_seconds, 0.9)
    derived = TARGET_CI_MULTIPLIER * p90 if p90 is not None else 0.0
    return {
        "target_seconds": max(TARGET_MIN_SECONDS, derived),
        "min_seconds": TARGET_MIN_SECONDS,
        "ci_multiplier": TARGET_CI_MULTIPLIER,
        "p90_batch_ci_seconds": p90,
        "batch_ci_samples": len(ci_seconds),
        "source": "derived" if p90 is not None else "floor",
    }


# ----------------------------------------------------------------- loading


def _payload(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _commits(raw):
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


async def _task_rows(conn, ids, columns):
    found = {}
    for table in (tasks, archived_tasks):
        missing = sorted(set(ids) - set(found))
        if not missing:
            break
        rows = await conn.execute(select(*(table.c[name] for name in columns)).where(
            table.c.id.in_(missing)))
        for row in rows.mappings():
            found[row["id"]] = {**row, "archived": table is archived_tasks}
    return found


async def _approval_anchors(conn, cohort_tasks):
    """Task id -> (anchor_at, anchor_kind) per revision 4.

    The nearest ancestor (or the task itself) filed by a spec ingest batch is
    anchored at that batch's approval decision; anything else at task creation.
    """
    parents = {tid: row["parent_task_id"] for tid, row in cohort_tasks.items()}
    created = {tid: row["created_at"] for tid, row in cohort_tasks.items()}
    frontier = {pid for pid in parents.values() if pid and pid not in parents}
    for _ in range(_ANCESTRY_DEPTH):
        if not frontier:
            break
        rows = await _task_rows(conn, frontier, ("id", "parent_task_id", "created_at"))
        for tid, row in rows.items():
            parents[tid] = row["parent_task_id"]
            created[tid] = row["created_at"]
        frontier = {pid for pid in (row["parent_task_id"] for row in rows.values())
                    if pid and pid not in parents}
    members = (await conn.execute(
        select(ingest_batch_members.c.task_id, ingest_batches.c.decision_id,
               ingest_batches.c.source_review_id, ingest_batches.c.created_at)
        .select_from(ingest_batch_members.join(
            ingest_batches, ingest_batches.c.batch_id == ingest_batch_members.c.batch_id))
        .where(ingest_batch_members.c.task_id.in_(sorted(parents)))
        .order_by(ingest_batches.c.created_at)
    )).mappings().all()
    filed = {}
    for row in members:
        filed.setdefault(row["task_id"], row)
    decision_ids = sorted({row["decision_id"] for row in filed.values() if row["decision_id"]})
    review_ids = sorted({row["source_review_id"] for row in filed.values()
                         if row["source_review_id"]})
    decided = dict((await conn.execute(select(ask_decisions.c.id, ask_decisions.c.created_at)
                                       .where(ask_decisions.c.id.in_(decision_ids)))).all())
    reviewed = dict((await conn.execute(select(doc_reviews.c.id, doc_reviews.c.decided_at)
                                        .where(doc_reviews.c.id.in_(review_ids)))).all())
    anchors = {}
    for tid in cohort_tasks:
        node, seen = tid, set()
        while node and node not in seen and node not in filed:
            seen.add(node)
            node = parents.get(node)
        row = filed.get(node) if node else None
        if row is None:
            anchors[tid] = (created.get(tid), "task_created")
        elif decided.get(row["decision_id"]) is not None:
            anchors[tid] = (decided[row["decision_id"]], "spec_approved")
        elif reviewed.get(row["source_review_id"]) is not None:
            anchors[tid] = (reviewed[row["source_review_id"]], "spec_approved")
        else:
            anchors[tid] = (row["created_at"], "spec_ingested")
    return anchors


async def load_inputs(conn, *, since: float, until: float, project_id: str | None = None):
    """Every row the aggregation needs, read once; nothing is written."""
    owner = tasks.alias("cohort_task")
    cohort_rows = []
    for table in (tasks, archived_tasks):
        query = (
            select(completions.c.id, completions.c.task_id, completions.c.completed_at,
                   completions.c.work_outcome, completions.c.commits, completions.c.branch,
                   table.c.project_id, table.c.status, table.c.created_at,
                   table.c.parent_task_id, table.c.branch_name)
            .select_from(completions.join(table, table.c.id == completions.c.task_id))
            .where(completions.c.outcome == "pass", completions.c.completed_at >= since,
                   completions.c.completed_at <= until)
        )
        if table is archived_tasks:
            query = query.where(~select(owner.c.id).where(owner.c.id == table.c.id).exists())
        if project_id is not None:
            query = query.where(table.c.project_id == project_id)
        cohort_rows.extend(dict(row, archived=table is archived_tasks)
                           for row in (await conn.execute(query)).mappings())
    task_ids = sorted({row["task_id"] for row in cohort_rows})
    project_ids = sorted({row["project_id"] for row in cohort_rows} | (
        {project_id} if project_id else set()))

    later = defaultdict(list)
    if task_ids:
        for tid, cid, at in (await conn.execute(
            select(completions.c.task_id, completions.c.id, completions.c.completed_at)
            .where(completions.c.task_id.in_(task_ids))
        )).all():
            later[tid].append((at, cid))

    observations = defaultdict(list)
    if task_ids:
        for tid, timestamp, raw in (await conn.execute(
            select(events.c.task_id, events.c.timestamp, events.c.payload)
            .where(events.c.event_type == EVENT_TYPE, events.c.project_id.in_(project_ids),
                   events.c.task_id.in_(task_ids))
            .order_by(events.c.id)
        )).all():
            payload = _payload(raw)
            payload["recorded_at"] = timestamp
            observations[(tid, payload.get("generation"))].append(payload)

    started_query = select(events.c.project_id, func.min(events.c.timestamp)).where(
        events.c.event_type == EVENT_TYPE).group_by(events.c.project_id)
    if project_id is not None:
        started_query = started_query.where(events.c.project_id == project_id)
    instrumented = dict((await conn.execute(started_query)).all())

    dev_query = select(projects.c.id, repos.c.default_branch).select_from(
        projects.join(repos, repos.c.id == projects.c.integration_repository_id))
    if project_id is not None:
        dev_query = dev_query.where(projects.c.id == project_id)
    dev_refs = {pid: "refs/heads/" + str(branch).removeprefix("refs/heads/")
                for pid, branch in (await conn.execute(dev_query)).all()}

    batch_tasks = defaultdict(set)
    if task_ids:
        for batch_id, tid in (await conn.execute(
            select(integration_batch_members.c.batch_id, integration_batch_members.c.task_id)
            .where(integration_batch_members.c.task_id.in_(task_ids))
        )).all():
            batch_tasks[batch_id].add(tid)
    repair_tasks = defaultdict(set)
    if batch_tasks:
        for repair_id, batch_id in (await conn.execute(
            select(repair_operations.c.id, repair_operations.c.batch_id)
            .where(repair_operations.c.batch_id.in_(sorted(batch_tasks)))
        )).all():
            repair_tasks[repair_id] |= batch_tasks[batch_id]
    owners = {tid: {tid} for tid in task_ids}
    owners.update(batch_tasks)
    owners.update(repair_tasks)

    linked_waits = []
    if task_ids:
        legacy_owner = func.split_part(waits.c.owner_id, ":", 2)
        rows = (await conn.execute(select(
            waits.c.id, waits.c.owner_kind, waits.c.owner_id, waits.c.origin,
            waits.c.condition_kind, waits.c.outcome, waits.c.created_at,
            waits.c.resolved_at, waits.c.cleared_at, waits.c.evidence_ref,
            waits.c.result_ref, waits.c.decision_reason, legacy_owner.label("legacy_owner"),
        ).where(or_(
            waits.c.owner_id.in_(sorted(owners)),
            and_(waits.c.owner_id.like("legacy-%"), legacy_owner.in_(task_ids)),
        )))).mappings().all()
        for row in rows:
            linked = set(owners.get(row["owner_id"], ()))
            if row["owner_id"].startswith("legacy-") and row["legacy_owner"] in owners:
                linked.add(row["legacy_owner"])
            if linked:
                linked_waits.append({**row, "tasks": linked,
                                     "repair": row["owner_id"] in repair_tasks})
    wait_ids = sorted(row["id"] for row in linked_waits)
    measured = defaultdict(list)
    if wait_ids:
        for row in (await conn.execute(select(
            measurements.c.wait_id, measurements.c.waited_seconds,
            measurements.c.setup_seconds, measurements.c.rebuild_seconds,
        ).where(measurements.c.wait_id.in_(wait_ids)))).mappings():
            measured[row["wait_id"]].append(dict(row))

    ci_query = select(measurements.c.project_id, measurements.c.waited_seconds).select_from(
        measurements.join(waits, waits.c.id == measurements.c.wait_id)).where(
        waits.c.origin == "integration", measurements.c.condition_kind.in_(sorted(CI_KINDS)),
        measurements.c.recorded_at >= until - TARGET_CI_LOOKBACK,
        measurements.c.recorded_at <= until)
    if project_id is not None:
        ci_query = ci_query.where(measurements.c.project_id == project_id)
    ci_seconds = defaultdict(list)
    for pid, seconds in (await conn.execute(ci_query)).all():
        ci_seconds[pid].append(float(seconds))

    member_count = select(func.count()).where(
        integration_batch_members.c.batch_id == integration_batches.c.id).scalar_subquery()
    batch_query = select(
        integration_batches.c.id, integration_batches.c.project_id,
        integration_batches.c.created_at, integration_batches.c.repair_attempt_count,
        member_count.label("members"),
    ).where(integration_batches.c.created_at >= since, integration_batches.c.created_at <= until)
    if project_id is not None:
        batch_query = batch_query.where(integration_batches.c.project_id == project_id)
    batches = [dict(row) for row in (await conn.execute(batch_query)).mappings()]

    anchors = await _approval_anchors(conn, {
        row["task_id"]: row for row in cohort_rows}) if cohort_rows else {}
    return {
        "cohort": cohort_rows,
        "completions": dict(later),
        "observations": dict(observations),
        "instrumented_at": instrumented,
        "dev_refs": dev_refs,
        "waits": linked_waits,
        "measurements": dict(measured),
        "ci_seconds": dict(ci_seconds),
        "batches": batches,
        "anchors": anchors,
        "project_ids": project_ids,
    }


# -------------------------------------------------------------- aggregation


def _wait_interval(wait, now):
    end = wait["resolved_at"] or wait["cleared_at"]
    if end is None:
        end = now if wait["outcome"] == "pending" else wait["created_at"]
    return wait["created_at"], end


def _segment(wait):
    if wait["condition_kind"] in CI_KINDS:
        return "ci_time"
    if wait["repair"]:
        return "repair"
    return KIND_SEGMENT.get(wait["condition_kind"], "other")


def _classify(row, inputs, *, now):
    """One cohort completion's sample, exclusion or pending account."""
    tid, generation = row["task_id"], row["id"]
    dev_ref = inputs["dev_refs"].get(row["project_id"])
    seen = inputs["observations"].get((tid, generation), [])
    on_dev = [obs for obs in seen if obs.get("target_ref") == dev_ref]
    elsewhere = [obs for obs in seen if obs.get("target_ref") != dev_ref]
    landed = next((obs for obs in on_dev if obs.get("state") == "contained"), None)
    parent = next((obs for obs in elsewhere if obs.get("state") == "contained"), None)
    latest = on_dev[-1] if on_dev else None
    last_seen = latest or (seen[-1] if seen else None)
    started = inputs["instrumented_at"].get(row["project_id"])
    newer = [cid for at, cid in inputs["completions"].get(tid, ())
             if cid != generation and (at, cid) > (row["completed_at"], generation)]
    commits = _commits(row["commits"])
    info = {
        "task_id": tid,
        "generation_id": generation,
        "project_id": row["project_id"],
        "completed_at": row["completed_at"],
        "status": None,
        "exclusion": None,
        "dev_ref": dev_ref,
        "latest_state": latest.get("state") if latest else None,
        "on_dev_at": None,
        "parent_at": _observed(parent) if parent else None,
        "parent_ref": parent.get("target_ref") if parent else None,
        "source_ref": row["branch"] or row["branch_name"],
        "source_oid": last_seen.get("source_oid") if last_seen else None,
        "target_oid": last_seen.get("target_oid") if last_seen else None,
        "observed_at": _observed(last_seen) if last_seen else None,
        "reason": last_seen.get("reason") if last_seen else None,
    }
    if any(obs.get("exclusion") == "delivered_by_other_means" for obs in seen):
        return {**info, "status": "excluded", "exclusion": "delivered_by_other_means"}
    if (latest and latest.get("state") in CODE_FREE_STATES) or (
        str(row["work_outcome"] or "").lower() in CODE_FREE_OUTCOMES
    ) or (not seen and commits == [] and not (row["branch"] or row["branch_name"])):
        return {**info, "status": "excluded", "exclusion": "code_free"}
    if landed is not None:
        on_dev_at = _observed(landed)
        censored = landed is on_dev[0] and (started is None or row["completed_at"] < started)
        return {**info, "status": "left_censored" if censored else "sample",
                "on_dev_at": on_dev_at,
                "current_blocker": latest is not None and latest.get("state") != "contained"}
    if row["status"] == "CANCELLED" or str(row["work_outcome"] or "").lower() == "abandoned":
        return {**info, "status": "excluded", "exclusion": "abandoned"}
    if newer:
        return {**info, "status": "excluded", "exclusion": "superseded"}
    if latest and latest.get("state") == "settled":
        return {**info, "status": "excluded", "exclusion": "settled"}
    if latest is None:
        live = started is not None and row["completed_at"] >= started
        return {**info, "status": "unknown" if live else "historical_unknown",
                "age_seconds": max(0.0, now - row["completed_at"])}
    return {**info, "status": "pending", "age_seconds": max(0.0, now - row["completed_at"])}


def _wait_view(wait):
    """One linked wait record as drill-down shows it; ``ci_url`` only for http(s)."""
    result_ref = wait.get("result_ref")
    ci = wait["condition_kind"] in CI_KINDS and str(result_ref or "").startswith(
        ("https://", "http://"))
    return {
        "wait_id": wait["id"],
        "origin": wait["origin"],
        "condition_kind": wait["condition_kind"],
        "segment": (_segment(wait) if wait["origin"] == "integration"
                    and wait["condition_kind"] != "human" else None),
        "outcome": wait["outcome"],
        "created_at": wait["created_at"],
        "resolved_at": wait["resolved_at"] or wait["cleared_at"],
        "evidence_ref": wait.get("evidence_ref"),
        "result_ref": result_ref,
        "decision_reason": wait.get("decision_reason"),
        "ci_url": result_ref if ci else None,
    }


def _observed(obs):
    value = obs.get("observed_at")
    return float(value if value is not None else obs.get("recorded_at"))


def _histogram(ages):
    edges = ((3_600, "<1h"), (21_600, "1-6h"), (86_400, "6-24h"), (604_800, "1-7d"))
    counts = {label: 0 for _edge, label in edges} | {">7d": 0}
    for age in ages:
        counts[next((label for edge, label in edges if age < edge), ">7d")] += 1
    return [{"bucket": label, "count": count} for label, count in counts.items()]


def aggregate(inputs, *, since: float, until: float, now: float, window: str,
              project_id: str | None = None) -> dict:
    """The cohort report; percentiles cover delivered samples only."""
    targets = {pid: delivery_target(inputs["ci_seconds"].get(pid, []))
               for pid in inputs["project_ids"]}
    waits_by_task = defaultdict(list)
    for wait in inputs["waits"]:
        for tid in wait["tasks"]:
            waits_by_task[tid].append(wait)

    generations, excluded = [], defaultdict(int)
    for row in sorted(inputs["cohort"], key=lambda item: (item["completed_at"], item["id"])):
        item = _classify(row, inputs, now=now)
        anchor_at, anchor_kind = inputs["anchors"].get(row["task_id"], (None, "task_created"))
        anchor_at = row["completed_at"] if anchor_at is None else min(
            float(anchor_at), row["completed_at"])
        item.update(anchor_at=anchor_at, anchor_kind=anchor_kind, clock_skew=False,
                    late=False, segments={}, residual_seconds=None,
                    human_wait_seconds=None, rebuild_seconds=None, setup_seconds=None,
                    cache_hit_rate=None, waits=[])
        if item["status"] == "excluded":
            excluded[item["exclusion"]] += 1
            generations.append(item)
            continue
        end = item["on_dev_at"] if item["on_dev_at"] is not None else now
        linked = waits_by_task.get(row["task_id"], [])
        intervals = defaultdict(list)
        human = []
        measured = []
        for wait in linked:
            span = _wait_interval(wait, now)
            if wait["condition_kind"] == "human":
                human.append(span)
            elif wait["origin"] == "integration":
                intervals[_segment(wait)].append(span)
                measured.extend(inputs["measurements"].get(wait["id"], ()))
        human_seconds = union_seconds(_clip(human, anchor_at, max(anchor_at, end)))
        if item["on_dev_at"] is not None:
            raw = item["on_dev_at"] - row["completed_at"]
            approval = item["on_dev_at"] - anchor_at - human_seconds
            item["clock_skew"] = raw < 0 or approval < 0
            item["completion_to_dev_seconds"] = max(0.0, raw)
            item["approval_to_dev_seconds"] = max(0.0, approval)
            item["late"] = item["on_dev_at"] > until
        total = max(0.0, end - anchor_at - human_seconds)
        clipped = {name: _clip(spans, anchor_at, max(anchor_at, end))
                   for name, spans in intervals.items()}
        item["segments"] = {name: union_seconds(spans) for name, spans in clipped.items()
                            if name in SEGMENTS}
        covered = union_seconds([span for spans in clipped.values() for span in spans])
        item["residual_seconds"] = max(0.0, total - covered)
        item["human_wait_seconds"] = human_seconds
        rebuilds = [m["rebuild_seconds"] for m in measured if m["rebuild_seconds"] is not None]
        setups = [m["setup_seconds"] for m in measured if m["setup_seconds"] is not None]
        item["rebuild_seconds"] = sum(rebuilds) if rebuilds else None
        item["setup_seconds"] = sum(setups) if setups else None
        item["cache_hit_rate"] = (sum(1 for value in rebuilds if value == 0) / len(rebuilds)
                                  if rebuilds else None)
        item["waits"] = [_wait_view(wait) for wait in sorted(linked, key=lambda wait: (
            wait["outcome"] != "pending", -wait["created_at"], wait["id"]))][:WAIT_LIMIT]
        item["live_wait"] = any(wait["outcome"] == "pending" and wait["cleared_at"] is None
                                for wait in linked)
        item["measurements"] = rebuilds
        generations.append(item)

    samples = [g for g in generations if g["status"] == "sample"]
    pending = [g for g in generations if g["status"] == "pending"]
    unknown = [g for g in generations if g["status"] == "unknown"]
    historical = [g for g in generations if g["status"] == "historical_unknown"]
    censored = [g for g in generations if g["status"] == "left_censored"]
    slow = stuck = 0
    for g in pending:
        if g["age_seconds"] > targets.get(g["project_id"], delivery_target([]))["target_seconds"]:
            g["watchdog"] = "slow" if g["live_wait"] else "stuck"
            slow, stuck = slow + g["live_wait"], stuck + (not g["live_wait"])
    within = [g for g in samples if g["approval_to_dev_seconds"] <= targets.get(
        g["project_id"], delivery_target([]))["target_seconds"]]
    approval = [g["approval_to_dev_seconds"] for g in samples]
    completion = [g["completion_to_dev_seconds"] for g in samples]
    parent = [max(0.0, g["parent_at"] - g["completed_at"]) for g in generations
              if g["parent_at"] is not None]
    eligible = len(generations) - sum(excluded.values())
    rebuilds = [value for g in samples + pending for value in g.pop("measurements", ())]
    for g in generations:
        g.pop("measurements", None)
        g.pop("live_wait", None)
    open_ages = [g["age_seconds"] for g in pending + unknown]
    per_day = defaultdict(lambda: {"batches": 0, "members": 0, "red_rounds": 0})
    for batch in inputs["batches"]:
        day = per_day[int(batch["created_at"] // 86_400) * 86_400]
        day["batches"] += 1
        day["members"] += int(batch["members"] or 0)
        day["red_rounds"] += int(batch["repair_attempt_count"] or 0)
    target = {"project_id": project_id, **targets[project_id]} if project_id in targets else None
    return {
        "observed_at": now,
        "window": window,
        "since": since,
        "until": until,
        "project_id": project_id,
        "metric": "approval_to_dev",
        "percentiles_exclude_pending": True,
        "median_seconds": quantile(approval, 0.5),
        "p90_seconds": quantile(approval, 0.9),
        "completion_to_dev_median_seconds": quantile(completion, 0.5),
        "completion_to_dev_p90_seconds": quantile(completion, 0.9),
        "sample_count": len(samples),
        "eligible_count": eligible,
        "pending_count": len(pending),
        "unknown_count": len(unknown),
        "historical_unknown_count": len(historical),
        "left_censored_count": len(censored),
        "slow_count": slow,
        "stuck_count": stuck,
        "late_sample_count": sum(1 for g in samples if g["late"]),
        "clock_skew_count": sum(1 for g in samples + censored if g["clock_skew"]),
        "current_blocker_count": sum(1 for g in samples if g.get("current_blocker")),
        "excluded": [{"reason": reason, "count": excluded.get(reason, 0)}
                     for reason in EXCLUSIONS],
        "oldest_pending_age_seconds": max(open_ages) if open_ages else None,
        "within_target_fraction": len(within) / len(samples) if samples else None,
        "coverage": len(samples) / eligible if eligible else None,
        "unknown_fraction": (len(unknown) + len(historical)) / eligible if eligible else None,
        "target": target,
        "targets": [{"project_id": pid, **value} for pid, value in sorted(targets.items())],
        "segments": [
            {"segment": name, "median_seconds": quantile(
                [g["segments"].get(name, 0.0) for g in samples], 0.5) if samples else None,
             "generations": sum(1 for g in samples if name in g["segments"])}
            for name in SEGMENTS
        ],
        "residual_median_seconds": quantile([g["residual_seconds"] for g in samples], 0.5),
        "rebuild_seconds_total": sum(rebuilds) if rebuilds else None,
        "cache_hit_rate": (sum(1 for value in rebuilds if value == 0) / len(rebuilds)
                           if rebuilds else None),
        "cache_hit_rate_derived": True,
        "parent_delay_median_seconds": quantile(parent, 0.5),
        "author_merge_median_seconds": quantile(
            [g["segments"]["author_merge"] for g in samples if "author_merge" in g["segments"]],
            0.5),
        "historical_age_histogram": _histogram(g["age_seconds"] for g in historical),
        "batches_per_day": [{"day": day, **counts} for day, counts in sorted(per_day.items())],
        "generations": [
            g for g in sorted(generations, key=lambda g: (
                g["status"] not in {"pending", "unknown"}, -(g.get("age_seconds") or 0),
                g["completed_at"]))
        ][:DRILLDOWN_LIMIT],
    }


async def snapshot(engine, *, window: str = "24h", project_id: str | None = None,
                   until: float | None = None, now: float) -> dict:
    """Read the cohort ending at ``until`` (default now) and aggregate it."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {sorted(WINDOWS)}")
    until = now if until is None else min(float(until), now)
    since = until - WINDOWS[window]
    async with engine.connect() as conn:
        inputs = await load_inputs(conn, since=since, until=until, project_id=project_id)
    return aggregate(inputs, since=since, until=until, now=now, window=window,
                     project_id=project_id)
