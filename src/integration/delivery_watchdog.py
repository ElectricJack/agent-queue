"""Delivery age and progress observations for the train policy.

These functions neither wake agents nor mutate delivery. Recovery decisions
belong to the reviewed policy; Git containment and canonical wait outcomes
remain the authority. A watchdog projection must never count its own blocker
row as evidence that a delivery producer is progressing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import wait_measurements, wait_records

logger = logging.getLogger(__name__)

SAMPLE_WINDOW_SECONDS = 7 * 24 * 3600
THRESHOLD_REFRESH_SECONDS = 24 * 3600


@dataclass(frozen=True)
class DeliveryThreshold:
    stuck_after_seconds: float
    min_seconds: float
    ci_multiplier: float
    p90_batch_ci_seconds: float | None
    sample_count: int
    measured_at: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def refresh_due(self, now: float) -> bool:
        return now < self.measured_at or now >= self.measured_at + THRESHOLD_REFRESH_SECONDS


def derive_threshold(
    durations: Iterable[float], *, now: float, min_seconds: float = 1800,
    ci_multiplier: float = 2,
) -> DeliveryThreshold:
    """Continuous p90, matching PostgreSQL percentile_cont, with a no-data floor."""
    for value in (now, min_seconds, ci_multiplier):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("threshold inputs must be finite numbers")
    if min_seconds <= 0 or ci_multiplier <= 0:
        raise ValueError("watchdog floor and CI multiplier must be positive")
    samples = sorted(float(v) for v in durations
                     if type(v) in (int, float) and math.isfinite(v) and v >= 0)
    p90 = None
    if samples:
        position = (len(samples) - 1) * 0.9
        lower, upper = math.floor(position), math.ceil(position)
        p90 = samples[lower] + (samples[upper] - samples[lower]) * (position - lower)
    return DeliveryThreshold(
        max(min_seconds, ci_multiplier * p90) if p90 is not None else min_seconds,
        min_seconds, ci_multiplier, p90, len(samples), now,
    )


async def measured_threshold_on(
    conn, project_id: str, *, now: float, min_seconds: float = 1800,
    ci_multiplier: float = 2,
) -> DeliveryThreshold:
    """Use only this project's trailing-week integration CI measurements.

    Origin belongs to the canonical wait, not to its source_class label. Agent
    job waits, other projects and future-dated samples cannot raise this SLO.
    The caller retains the returned inputs and recomputes them once per day.
    """
    durations = (await conn.execute(select(wait_measurements.c.waited_seconds).join(
        wait_records, wait_records.c.id == wait_measurements.c.wait_id,
    ).where(
        wait_measurements.c.project_id == project_id,
        wait_records.c.project_id == project_id,
        wait_records.c.origin == "integration",
        wait_measurements.c.condition_kind.in_(("ci_run", "job")),
        wait_records.c.condition_kind == wait_measurements.c.condition_kind,
        wait_measurements.c.recorded_at >= now - SAMPLE_WINDOW_SECONDS,
        wait_measurements.c.recorded_at <= now,
    ))).scalars()
    return derive_threshold(
        durations, now=now, min_seconds=min_seconds, ci_multiplier=ci_multiplier,
    )


# -- the scan ---------------------------------------------------------------
#
# Mechanism only: the scan classifies, projects one blocker row per stuck
# generation into wait_records (so ``aq wait explain --task`` is the single
# view) and, after two stuck scans past the threshold, hands the generation to
# an agent once. The agent lands it (merge, fix, push when green); nothing here
# retries delivery itself or bypasses a hold.

CURSOR_KEY = "delivery_watchdog.cursor"
THRESHOLD_KEY = "delivery_watchdog.threshold:"
PAGE_SIZE = 50
STUCK_SCANS_TO_REDRIVE = 2
INCIDENT_SOURCE_KIND = "integration_delivery_stuck"

#: Inventory rule -> (state before ageing, reason code). Rules absent here are
#: contained or owe no default-branch delivery.
_WATCHED = {
    "carried": ("progressing", "batch_queued"),
    "awaiting_delivery": ("progressing", "candidate_ci_pending"),
    "stranded_root": ("stuck", "publisher_unavailable"),
    "unreconciled": ("stuck", "publisher_unavailable"),
    "unknown": ("unknown", "delivery_observation_unavailable"),
}


def _digest(text: str, size: int = 16) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:size]


def _policy_options(raw: Any) -> dict[str, Any]:
    """The project's ``delivery_watchdog`` override; defaults when absent or invalid."""
    from pydantic import ValidationError

    from src.integration.models import DeliveryWatchdogPolicy

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    try:
        return DeliveryWatchdogPolicy.model_validate(
            (raw or {}).get("delivery_watchdog") or {} if isinstance(raw, Mapping) else {}
        ).model_dump()
    except ValidationError:
        return DeliveryWatchdogPolicy().model_dump()


async def _config_get(conn, key: str) -> Any:
    from src.database.tables import system_config

    value = await conn.scalar(select(system_config.c.value).where(system_config.c.key == key))
    return json.loads(value) if value else None


async def _config_put(conn, key: str, value: Any) -> None:
    from src.database.tables import system_config

    text = json.dumps(value, sort_keys=True)
    await conn.execute(insert(system_config).values(key=key, value=text).on_conflict_do_update(
        index_elements=["key"], set_={"value": text},
    ))


async def project_threshold_on(conn, project_id: str, policy: Any, *, now: float
                               ) -> DeliveryThreshold:
    """The stored daily threshold, re-measured when due or when the override changed."""
    options = _policy_options(policy)
    fixed = options.pop("stuck_after_seconds")
    stored = await _config_get(conn, THRESHOLD_KEY + project_id)
    threshold = None
    if isinstance(stored, dict):
        try:
            threshold = DeliveryThreshold(**stored)
        except TypeError:
            threshold = None
    if (threshold is None or threshold.refresh_due(now)
            or (threshold.min_seconds, threshold.ci_multiplier)
            != (options["min_seconds"], options["ci_multiplier"])):
        threshold = await measured_threshold_on(conn, project_id, now=now, **options)
        await _config_put(conn, THRESHOLD_KEY + project_id, threshold.as_dict())
    if fixed is not None:
        from dataclasses import replace

        threshold = replace(threshold, stuck_after_seconds=float(fixed))
    return threshold


async def _held_on(conn, project_id: str, task_id: str, status: str | None) -> bool:
    """Labels, manual pause, PAUSED and active operator holds all stop the watchdog."""
    from src.database.tables import task_labels, task_metadata
    from src.operator_decisions import history_on, related_on

    if status == "PAUSED":
        return True
    if await conn.scalar(select(task_labels.c.task_id).where(
        task_labels.c.task_id == task_id, task_labels.c.label.like("hold:%"),
    ).limit(1)):
        return True
    pause = await conn.scalar(select(task_metadata.c.value).where(
        task_metadata.c.task_id == task_id, task_metadata.c.key == "manual_pause",
    ))
    if pause and pause not in ("false", "null", "0", '""'):
        return True
    refs = await related_on(conn, "task", task_id)
    return any(row["active"] for row in await history_on(conn, project_id, refs))


async def _task_status_on(conn, task_id: str) -> str | None:
    from src.database.tables import archived_tasks, tasks

    for table in (tasks, archived_tasks):
        status = await conn.scalar(select(table.c.status).where(table.c.id == task_id))
        if status is not None:
            return status
    return None


def _takeover_progress(status: str | None) -> tuple[str, str]:
    """The landing agent's task decides progress once the generation is handed over."""
    if status == "COMPLETED":
        return "progressing", "batch_queued"
    if status in (None, "FAILED", "BLOCKED"):
        return "stuck", "repair_deferred"
    if status in ("READY", "DEFINED"):
        return "progressing", "fixer_unclaimed"
    return "progressing", "fixer_running"


async def _pending_rows_on(conn, project_id: str) -> list[dict[str, Any]]:
    rows = (await conn.execute(select(wait_records).where(
        wait_records.c.project_id == project_id,
        wait_records.c.origin == "integration",
        wait_records.c.owner_kind == "scheduler",
        wait_records.c.outcome == "pending",
    ))).mappings().all()
    return [dict(row) for row in rows
            if (row["decision_inputs"] or {}).get("delivery_watchdog_projection")]


async def _settle_on(db, conn, row: Mapping[str, Any], *, now: float, outcome: str) -> None:
    """Containment satisfies the projection and resolves its incident."""
    from src.database.queries.escalation_queries import OPEN_ESCALATION_STATES
    from src.database.tables import escalations
    from src.waits.adapters.legacy_escalation import execute_mutation

    inputs = row["decision_inputs"] or {}
    await conn.execute(update(wait_records).where(
        wait_records.c.id == row["id"], wait_records.c.outcome == "pending",
    ).values(outcome="satisfied", resolved_at=now, cleared_at=now,
             version=wait_records.c.version + 1))
    if inputs.get("incident_id"):
        await execute_mutation(conn, update(escalations).where(
            escalations.c.id == inputs["incident_id"],
            escalations.c.source_kind == INCIDENT_SOURCE_KIND,
            escalations.c.state.in_(tuple(OPEN_ESCALATION_STATES)),
        ).values(
            state="resolved", revision=escalations.c.revision + 1, updated_at=now,
            terminal_at=now, terminal_outcome=outcome, outcome="sweep",
            terminal_evidence={"task_id": row["owner_id"], "head": inputs.get("head"),
                               "target_ref": inputs.get("target_ref"),
                               "source_generation": row["source_generation"],
                               "automatic_rule": "delivery_contained"},
        ))
    if inputs.get("redrive"):
        await db.log_event("delivery.recovered", project_id=row["project_id"],
                           task_id=row["owner_id"], payload=json.dumps({
                               "generation": row["condition"].get("generation"),
                               "takeover_task_id": inputs["redrive"]["task_id"],
                               "outcome": outcome}, sort_keys=True), conn=conn)


async def _mark_rows_on(conn, rows, *, reason: str, verification: str, now: float) -> None:
    for row in rows:
        inputs = {**(row["decision_inputs"] or {}), "delivery_state": "unknown",
                  "delivery_reason": reason, "stuck_scans": 0}
        await conn.execute(update(wait_records).where(wait_records.c.id == row["id"]).values(
            decision_reason=reason, decision_inputs=inputs, verification_state=verification,
            verified_at=now, version=wait_records.c.version + 1,
        ))


async def _file_takeover_on(db, conn, *, key: str, entry: Mapping[str, Any],
                            repository_id: str, target_ref: str, hierarchy=None) -> str:
    """One agent task per generation lands the stuck work; the id makes it idempotent."""
    from src.integration.models import INTEGRATION_REPAIR_PRIORITY
    from src.models import Task, TaskStatus, TaskType

    task_id = "land-" + _digest(key)
    if await _task_status_on(conn, task_id) is not None:
        return task_id
    target = target_ref.removeprefix("refs/heads/")
    await db.create_task(Task(
        id=task_id, project_id=entry["project_id"], repo_id=repository_id,
        title=f"Land stuck delivery of {entry['task_id']}",
        description=(
            f"Task {entry['task_id']} completed, but its branch `{entry['branch']}` "
            f"(head {entry['head']}) has not reached `{target}` and no delivery owner is "
            f"moving it: {entry['reason']}.\n\nTake it over and land it: merge "
            f"`origin/{entry['branch']}` onto `origin/{target}` in this task's branch, "
            "resolve conflicts, fix what fails, run the checks and push when green; the "
            "integration train delivers it from there. Do not retry the old delivery. "
            "If the work should not land, say so in the close summary and fail the task."
        ),
        branch_name="aq/" + task_id, status=TaskStatus.READY, task_type=TaskType.BUGFIX,
        priority=INTEGRATION_REPAIR_PRIORITY, dedup_key=key,
        created_by_kind="system", created_by_id="delivery-watchdog",
    ), conn=conn)
    if hierarchy is not None:
        await hierarchy.reserve_ready_root_origin_on(conn, task_id)
    return task_id


async def _raise_incident_on(db, conn, *, key: str, entry: Mapping[str, Any],
                             inputs: Mapping[str, Any], now: float) -> str:

    from src.database.tables import messages

    project_id, task_id = entry["project_id"], entry["task_id"]
    incident_id = "escalation-delivery-stuck-" + _digest(key, 24)
    summary = (f"Delivery of {task_id} is stuck ({inputs['delivery_reason']}) after an "
               "agent takeover was requested")
    decision = (f"Run `aq wait explain --task {task_id}` and decide: unblock or reassign the "
                "landing task, land the branch by hand, or abandon the generation. The "
                "incident resolves itself once the work is on the default branch.")
    incident, created = await db.create_escalation(
        conn=conn, id=incident_id, project_id=project_id, task_id=task_id,
        source_kind=INCIDENT_SOURCE_KIND, source_identity=key,
        incident_key="delivery-stuck:" + key, supervisor_owner=f"supervisor-{project_id}",
        task_title=f"Land stuck delivery of {task_id}", summary=summary,
        investigation=json.dumps({**inputs, "entry": dict(entry)}, sort_keys=True)[:8000],
        decision_requested=decision, severity="high", now=now,
    )
    if created:
        await conn.execute(insert(messages).values(
            id="msg-" + incident_id, project_id=project_id, from_kind="system",
            from_id="delivery-watchdog", to_kind="session", to_id=incident["supervisor_owner"],
            subject=summary, body=f"{summary}\n{decision}\nIncident: {incident_id}",
            thread_id=incident_id, priority=100, created_at=now, archive_after_inject=1,
            body_kind=INCIDENT_SOURCE_KIND,
        ).on_conflict_do_nothing(index_elements=[messages.c.id]))
    return incident_id


async def _watch_entry(db, entry: Mapping[str, Any], request: Any, previous: Any, *,
                       threshold: DeliveryThreshold, repository_id: str, target_ref: str,
                       now: float, hierarchy=None) -> dict[str, Any]:
    """Classify one owed generation and write its projection; at most one handover."""
    from src.metrics.delivery_latency import generation_of

    task_id, project_id = entry["task_id"], entry["project_id"]
    generation = (generation_of(request) if request is not None else None) or (
        "head:" + entry["head"])
    key = f"{repository_id}:{task_id}:{generation}:{target_ref}"

    completed_at = request.completed_at if request is not None else None
    state, reason = _WATCHED[entry["rule"]]
    if (entry["rule"] == "unreconciled" and request is not None
            and not request.has_recorded_source):
        reason = "missing_source_provenance"
    async with db.immediate() as conn:
        # Batch sealing takes this same fence. Recheck incoming ownership in
        # the write transaction even if inventory observed an unowned epic.
        await db.lock_hierarchy_project(conn, project_id)
        wait_id = "delivery-watchdog-" + _digest(key, 24)
        if previous is not None and previous["id"] != wait_id:
            await _settle_on(db, conn, previous, now=now, outcome="superseded by a newer "
                             "completion generation")
        # The pending-only scan snapshot can omit a terminal projection or race
        # another scan. Reuse its identity and lock its current inputs before
        # counting scans or handing over; never reset a committed wait outcome.
        await conn.execute(insert(wait_records).values(
            id=wait_id, project_id=project_id,
            owner_kind="scheduler", owner_id=task_id, origin="integration",
            condition_kind="task", condition={"kind": "task", "task_id": task_id,
                                              "generation": generation,
                                              "target_ref": target_ref},
            source_key="delivery-watchdog:" + key, source_generation=0,
            source_watermark_seq=0, created_at=now, request_hash=_digest(key, 32),
            verification_state="verified", evidence_ref=entry["head"], verified_at=now,
        ).on_conflict_do_nothing())
        row = (await conn.execute(select(wait_records).where(
            wait_records.c.id == wait_id,
        ).with_for_update())).mappings().one()
        inputs = dict(row["decision_inputs"] or {})

        # Supervisor withdrawal is scoped to this exact generation. Re-read
        # the durable incident even when the pending snapshot omitted this row,
        # so a later scan or daemon restart cannot reopen the false alarm.
        if incident_id := inputs.get("incident_id"):
            from src.database.tables import escalations

            cancelled = await conn.scalar(select(escalations.c.id).where(
                escalations.c.id == incident_id, escalations.c.project_id == project_id,
                escalations.c.source_kind == INCIDENT_SOURCE_KIND,
                escalations.c.source_identity == key, escalations.c.state == "cancelled",
            ))
            if cancelled:
                inputs.update(delivery_state="cleared", delivery_reason="supervisor_cancelled",
                              stuck_scans=0)
                if row["outcome"] != "cancelled":
                    await conn.execute(update(wait_records).where(
                        wait_records.c.id == wait_id,
                    ).values(outcome="cancelled", resolved_at=now, cleared_at=now,
                             decision_reason="supervisor_cancelled", decision_inputs=inputs,
                             version=wait_records.c.version + 1))
                return {"task_id": task_id, "generation": generation, "state": "cleared",
                        "reason": "supervisor_cancelled", "age_seconds": inputs.get("age_seconds"),
                        "stuck_scans": 0, "redrive": inputs.get("redrive"),
                        "incident_id": incident_id}

        first = inputs.get("first_completed_at") or completed_at or now
        age = max(0.0, now - first)
        if state == "progressing" and age >= threshold.stuck_after_seconds:
            state = "slow"
        elif state == "stuck" and age < threshold.stuck_after_seconds:
            state = "progressing"
        redrive = inputs.get("redrive")
        if redrive:
            state, reason = _takeover_progress(await _task_status_on(conn, redrive["task_id"]))
        from src.database.queries.integration_train_queries import open_target_batches

        incoming = await conn.scalar(select(open_target_batches(
            project_id, repository_id,
            "refs/heads/" + entry["branch"].removeprefix("refs/heads/"),
        ).exists()))
        if incoming:
            state, reason = "progressing", "batch_queued"
        status = request.task_status if request is not None else None
        if await _held_on(conn, project_id, task_id, status):
            state, reason = "held", "manual_hold"
        stuck_scans = inputs.get("stuck_scans", 0) + 1 if state == "stuck" else 0
        incident_id, refused = inputs.get("incident_id"), None
        if not redrive and state == "stuck" and stuck_scans >= STUCK_SCANS_TO_REDRIVE:
            try:
                async with conn.begin_nested():
                    redrive = {"task_id": await _file_takeover_on(
                        db, conn, key=key, entry=entry, repository_id=repository_id,
                        target_ref=target_ref, hierarchy=hierarchy), "at": now}
            except Exception as exc:  # noqa: BLE001 - a refusal is the incident's evidence
                refused = f"{type(exc).__name__}: {exc}"[:500]
        inputs.update(
            delivery_watchdog_projection=True, delivery_state=state, delivery_reason=reason,
            stuck_scans=stuck_scans, first_completed_at=first, threshold=threshold.as_dict(),
            redrive=redrive, repository_id=repository_id, target_ref=target_ref,
            head=entry["head"], age_seconds=age, rule=entry["rule"],
        )
        if refused:
            inputs["redrive_refused"] = refused
        if not incident_id and state != "held" and (refused or (redrive and (
                reason == "repair_deferred"
                or now >= redrive["at"] + threshold.stuck_after_seconds))):
            incident_id = await _raise_incident_on(db, conn, key=key, entry=entry,
                                                   inputs=inputs, now=now)
        inputs["incident_id"] = incident_id
        await conn.execute(update(wait_records).where(wait_records.c.id == wait_id).values(
            decision_reason=reason, decision_inputs=inputs, verification_state="verified",
            evidence_ref=entry["head"], verified_at=now, outcome="pending", resolved_at=None, cleared_at=None,
            version=wait_records.c.version + 1,

        ))
    return {"task_id": task_id, "generation": generation, "state": state, "reason": reason,
            "age_seconds": age, "stuck_scans": stuck_scans, "redrive": redrive,
            "incident_id": incident_id}


async def scan_stuck_deliveries(db, *, observer, now: float, observe=None,
                                page_size: int = PAGE_SIZE, hierarchy=None) -> dict[str, Any]:
    """One bounded, oldest-first pass over projects after the persisted cursor.

    Projects are scanned whole, so an owed generation missing from a scanned
    project's inventory is contained and its projection settles. An unavailable
    repository still advances the cursor and never counts as stuck. Each task
    commits separately; failures are reported and retried on a later pass.
    """
    from src.database.tables import projects, repos
    from src.integration.delivery_branches import DELIVERING_MODES
    from src.integration.delivery_truth import load_delivery_requests

    if observe is None:
        from functools import partial

        from src.integration.delivery_branches import observe_completed_branches as observe

        # Recovery files work against the current epic tip, which can advance
        # after its descriptive completion record was written.
        observe = partial(observe, max_age=0.0)
    async with db._engine.connect() as conn:
        targets = [dict(row) for row in (await conn.execute(
            select(projects.c.id, projects.c.status,
                   projects.c.hierarchical_integration_mode.label("mode"),
                   projects.c.hierarchical_integration_policy.label("policy"),
                   repos.c.id.label("repository_id"), repos.c.default_branch)
            .select_from(projects.join(repos, repos.c.id == projects.c.integration_repository_id))
            .order_by(projects.c.id)
        )).mappings()]
        cursor = await _config_get(conn, CURSOR_KEY)
    ordered = ([t for t in targets if cursor is None or t["id"] > cursor]
               + [t for t in targets if cursor is not None and t["id"] <= cursor])
    report: dict[str, Any] = {"projects": [], "deliveries": [], "unavailable": [],
                              "train_disabled": [], "project_inactive": [], "errors": []}
    seen = 0
    for target in ordered:
        if seen >= page_size:
            break
        project_id = target["id"]
        report["projects"].append(project_id)
        async with db.immediate() as conn:
            pending = {row["owner_id"]: row for row in await _pending_rows_on(conn, project_id)}
            if target["status"] != "ACTIVE":
                report["project_inactive"].append(project_id)
                await _mark_rows_on(conn, pending.values(),
                                    reason="project_" + target["status"].lower(),
                                    verification="verified", now=now)
                await _config_put(conn, CURSOR_KEY, project_id)
                continue
            if target["mode"] not in DELIVERING_MODES:
                report["train_disabled"].append(project_id)
                await _mark_rows_on(conn, pending.values(), reason="train_disabled",
                                    verification="verified", now=now)
                await _config_put(conn, CURSOR_KEY, project_id)
                continue
        observed = await observe(db, observer, project_ids={project_id}, now=now)
        if observed.get("unavailable"):
            report["unavailable"].append(project_id)
            async with db.immediate() as conn:
                await _mark_rows_on(conn, pending.values(), reason="target_unavailable",
                                    verification="unavailable", now=now)
                await _config_put(conn, CURSOR_KEY, project_id)
            continue
        entries = [e for e in observed.get("entries", ())
                   if e["project_id"] == project_id and e["rule"] in _WATCHED
                   and not e["archived"] and not e["wip"]]
        target_ref = "refs/heads/" + (target["default_branch"] or "").removeprefix("refs/heads/")
        requests = await load_delivery_requests(
            db, [e["task_id"] for e in entries], repository_id=target["repository_id"],
            target_ref=target_ref, reduced=True,
        ) if entries else {}
        async with db.immediate() as conn:
            threshold = await project_threshold_on(conn, project_id, target["policy"], now=now)
        entries.sort(key=lambda e: ((requests.get(e["task_id"]) and
                                     requests[e["task_id"]].completed_at) or now, e["task_id"]))
        for entry in entries:
            seen += 1
            previous = pending.pop(entry["task_id"], None)
            try:
                report["deliveries"].append(await _watch_entry(
                    db, entry, requests.get(entry["task_id"]), previous,
                    threshold=threshold, repository_id=target["repository_id"],
                    target_ref=target_ref, now=now, hierarchy=hierarchy,
                ))
            except Exception as exc:
                logger.exception("delivery watchdog entry failed for %s/%s",
                                 project_id, entry["task_id"])
                report["errors"].append({"project_id": project_id, "task_id": entry["task_id"],
                                         "error": type(exc).__name__})
        for row in pending.values():
            try:
                async with db.immediate() as conn:
                    await _settle_on(db, conn, row, now=now,
                                     outcome="the completed work reached the default branch")
            except Exception as exc:
                logger.exception("delivery watchdog settlement failed for %s/%s",
                                 project_id, row["owner_id"])
                report["errors"].append({"project_id": project_id, "task_id": row["owner_id"],
                                         "error": type(exc).__name__})

        async with db.immediate() as conn:
            await _config_put(conn, CURSOR_KEY, project_id)
    return report
