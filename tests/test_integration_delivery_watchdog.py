"""Threshold and producer progress evidence for the delivery watchdog."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database import Database
from src.database.tables import (
    escalations,
    events,
    messages,
    projects,
    system_config,
    task_labels,
    tasks,
    wait_measurements,
    wait_records,
)
from src.integration import delivery_truth
from src.integration.delivery_watchdog import (
    CURSOR_KEY,
    THRESHOLD_KEY,
    classify_delivery_progress,
    derive_threshold,
    measured_threshold_on,
    scan_stuck_deliveries,
)
from src.integration.models import DeliveryWatchdogPolicy
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.db_fixtures import lease_dsn


@pytest.fixture
def clock():
    return [1_800_000_000.0]


def test_threshold_uses_floor_without_measurements(clock):
    threshold = derive_threshold([], now=clock[0])
    assert threshold.stuck_after_seconds == 1800
    assert threshold.p90_batch_ci_seconds is None
    assert threshold.sample_count == 0


def test_threshold_follows_long_project_ci(clock):
    threshold = derive_threshold([2400] * 10, now=clock[0])
    assert threshold.stuck_after_seconds == 4800
    assert threshold.p90_batch_ci_seconds == 2400
    assert threshold.sample_count == 10


def test_threshold_continuous_percentile_and_project_overrides(clock):
    threshold = derive_threshold([100, 200, 300], now=clock[0],
                                 min_seconds=60, ci_multiplier=3)
    assert threshold.p90_batch_ci_seconds == 280
    assert threshold.stuck_after_seconds == 840


def test_invalid_measurements_cannot_poison_threshold(clock):
    threshold = derive_threshold([True, -1, float("nan"), float("inf"), 0, 10], now=clock[0])
    assert threshold.sample_count == 2
    assert threshold.stuck_after_seconds == 1800


@pytest.mark.parametrize("options", [{"min_seconds": 0}, {"ci_multiplier": -1},
                                     {"min_seconds": True}, {"ci_multiplier": float("nan")}])
def test_invalid_threshold_configuration_refused(clock, options):
    with pytest.raises(ValueError):
        derive_threshold([], now=clock[0], **options)


def test_threshold_daily_refresh_boundary_and_clock_reversal(clock):
    threshold = derive_threshold([], now=clock[0])
    assert not threshold.refresh_due(clock[0] + 86399)
    assert threshold.refresh_due(clock[0] + 86400)
    assert threshold.refresh_due(clock[0] - 1)


def progress(clock, wait, *, age=3600, escalation=None):
    return classify_delivery_progress(
        completed_at=clock[0] - age, now=clock[0],
        threshold=derive_threshold([], now=clock[0]), wait=wait, escalation=escalation,
        decision_budget_seconds=300,
    )


@pytest.fixture
def producer(clock):
    return {"id": "ci-wait", "origin": "integration", "outcome": "pending",
            "verification_state": "verified", "escalate_at": clock[0] + 60,
            "decision_inputs": {"delivery_reason": "candidate_ci_pending"}}


def test_progressing_ci_is_slow_and_preserves_first_completion_age(clock, producer):
    result = progress(clock, producer)
    assert (result.state, result.reason, result.age_seconds, result.wait_id) == (
        "slow", "candidate_ci_pending", 3600, "ci-wait",
    )
    assert progress(clock, producer, age=60).state == "progressing"


def test_absent_or_unavailable_producer_is_stuck(clock, producer):
    assert progress(clock, None).reason == "publisher_unavailable"
    result = progress(clock, {**producer, "outcome": "source_unavailable"})
    assert (result.state, result.reason) == ("stuck", "target_unavailable")


def test_agent_wait_does_not_supply_delivery_ownership(clock, producer):
    assert progress(clock, {**producer, "origin": "agent"}).state == "stuck"


def test_watchdog_blocker_cannot_make_itself_progressing(clock, producer):
    producer["decision_inputs"].update(delivery_watchdog_projection=True,
                                      delivery_reason="manual_hold")
    result = progress(clock, producer)
    assert (result.state, result.reason) == ("stuck", "manual_hold")


def test_supervisor_decision_budget_uses_committed_escalation(clock, producer):
    producer["escalate_at"] = clock[0] - 300
    escalation = {"raised_at": clock[0] - 299, "decided_at": None}
    assert progress(clock, producer, escalation=escalation).state == "progressing"
    escalation["raised_at"] -= 1
    result = progress(clock, producer, escalation=escalation)
    assert (result.state, result.reason) == ("stuck", "awaiting_supervisor_decision")


def test_decided_escalation_uses_current_extended_deadline(clock, producer):
    escalation = {"raised_at": clock[0] - 3600, "decided_at": clock[0] - 1}
    assert progress(clock, producer, escalation=escalation).state == "slow"


@pytest.mark.parametrize("change", [{"verification_state": "stale"},
                                     {"escalate_at": None}, {"escalate_at": 0}])
def test_incomplete_observation_is_never_recovery_authority(clock, producer, change):
    result = progress(clock, {**producer, **change})
    assert (result.state, result.reason) == ("unknown", "delivery_observation_unavailable")


def test_slow_threshold_can_vary_without_changing_completion_age(clock, producer):
    threshold = replace(derive_threshold([], now=clock[0]), stuck_after_seconds=4800)
    result = classify_delivery_progress(completed_at=clock[0] - 3600, now=clock[0],
                                       threshold=threshold, wait=producer,
                                       decision_budget_seconds=300)
    assert result.state == "progressing"
    assert result.age_seconds == 3600


async def test_measured_threshold_excludes_foreign_non_ci_agent_old_and_future_samples(clock):
    db = Database(lease_dsn("delivery-watchdog"))
    await db.initialize()
    try:
        for project_id in ("p", "q"):
            await db.create_project(Project(id=project_id, name=project_id))
        samples = [
            ("p", "integration", "ci_run", 2400, 0),
            ("p", "integration", "job", 2400, -7 * 86400),
            ("p", "agent", "job", 100000, 0),
            ("q", "integration", "ci_run", 100000, 0),
            ("p", "integration", "timer", 100000, 0),
            ("p", "integration", "ci_run", 100000, -7 * 86400 - 1),
            ("p", "integration", "job", 100000, 1),
        ]
        async with db.immediate() as conn:
            for index, (project_id, origin, kind, duration, offset) in enumerate(samples):
                wait_id = f"measurement-{index}"
                await conn.execute(insert(wait_records).values(
                    id=wait_id, project_id=project_id, owner_kind="integration",
                    owner_id=wait_id, origin=origin, condition_kind=kind,
                    condition={"kind": kind}, source_key=wait_id, source_generation=0,
                    source_watermark_seq=0, verification_state="verified", evidence_ref=wait_id,
                    verified_at=clock[0], created_at=clock[0] - duration,
                    outcome="satisfied", resolved_at=clock[0], request_hash=wait_id,
                ))
                await conn.execute(insert(wait_measurements).values(
                    id=wait_id, project_id=project_id, condition_kind=kind,
                    source_class="fixture", wait_id=wait_id, decision="in_place",
                    waited_seconds=duration, hibernated=False, evicted=False,
                    recorded_at=clock[0] + offset,
                ))
            threshold = await measured_threshold_on(conn, "p", now=clock[0])
        assert threshold.sample_count == 2
        assert threshold.stuck_after_seconds == 4800
    finally:
        await db.close()


# -- the scan: fake clock, fixture inventory and CI durations ---------------

class Inventory:
    """The Git inventory per project: entries, or an unavailable repository."""

    def __init__(self):
        self.entries, self.unavailable, self.calls = {}, set(), []

    def owe(self, project_id, task_id, rule="stranded_root"):
        self.entries.setdefault(project_id, {})[task_id] = {
            "task_id": task_id, "project_id": project_id, "archived": False, "root": True,
            "branch": f"aq/{task_id}", "head": "a" * 40, "wip": False, "rule": rule,
            "accounted": False, "reason": "fixture", "remedy": None,
        }

    async def __call__(self, db, observer, *, project_ids, now):
        (project_id,) = project_ids
        self.calls.append(project_id)
        if project_id in self.unavailable:
            return {"entries": [], "unavailable": [{"project_id": project_id}], "projects": 1}
        return {"entries": list(self.entries.get(project_id, {}).values()),
                "unavailable": [], "projects": 1}


@pytest.fixture
async def watch(clock, monkeypatch):
    db = Database(lease_dsn("delivery-watchdog-scan"))
    await db.initialize()
    state = SimpleNamespace(db=db)
    inventory = Inventory()
    completions = {}

    async def requests(_db, task_ids, **_):
        return {task_id: SimpleNamespace(
            completion_id=f"completion-{task_id}", legacy_generation="",
            completed_at=completions[task_id], has_recorded_source=True,
            task_status="COMPLETED") for task_id in task_ids if task_id in completions}

    monkeypatch.setattr(delivery_truth, "load_delivery_requests", requests)

    async def restart():
        """A new daemon: a fresh adapter on the same database, no memory in between."""
        await state.db.close()
        state.db = Database(lease_dsn("delivery-watchdog-scan"))
        await state.db.initialize()

    async def project(project_id, mode="train"):
        db = state.db
        await db.create_project(Project(id=project_id, name=project_id))
        await db.create_repo(RepoConfig(id=f"repo-{project_id}", project_id=project_id,
                                        source_type=RepoSourceType.LINK))
        await db.update_project(project_id, hierarchical_integration_mode=mode,
                                integration_repository_id=f"repo-{project_id}")

    async def complete(project_id, task_id, *, age, rule="stranded_root"):
        await state.db.create_task(Task(id=task_id, project_id=project_id, title=task_id,
                                  description="", status=TaskStatus.COMPLETED,
                                  branch_name=f"aq/{task_id}"))
        completions[task_id] = clock[0] - age
        inventory.owe(project_id, task_id, rule)

    async def scan(advance=60, **options):
        clock[0] += advance
        return await scan_stuck_deliveries(state.db, observer=object(), now=clock[0],
                                           observe=inventory, **options)

    async def rows(table, *where):
        async with state.db._engine.connect() as conn:
            return [dict(r) for r in (await conn.execute(select(table).where(*where))).mappings()]

    async def blocker(task_id):
        found = await rows(wait_records, wait_records.c.owner_id == task_id,
                           wait_records.c.owner_kind == "scheduler")
        assert len(found) == 1
        return found[0]

    await project("p")
    try:
        yield SimpleNamespace(state=state, inventory=inventory, project=project,
                              complete=complete, scan=scan, rows=rows, blocker=blocker,
                              restart=restart)
    finally:
        await state.db.close()


async def _ci_samples(db, project_id, durations, now):
    async with db.immediate() as conn:
        for index, duration in enumerate(durations):
            wait_id = f"ci-{project_id}-{index}"
            await conn.execute(insert(wait_records).values(
                id=wait_id, project_id=project_id, owner_kind="integration", owner_id=wait_id,
                origin="integration", condition_kind="ci_run", condition={"kind": "ci_run"},
                source_key=wait_id, source_generation=0, source_watermark_seq=0,
                verification_state="verified", evidence_ref=wait_id, verified_at=now,
                created_at=now - duration, outcome="satisfied", resolved_at=now,
                request_hash=wait_id,
            ))
            await conn.execute(insert(wait_measurements).values(
                id=wait_id, project_id=project_id, condition_kind="ci_run",
                source_class="fixture", wait_id=wait_id, decision="in_place",
                waited_seconds=duration, hibernated=False, evicted=False, recorded_at=now,
            ))


def _takeovers(found):
    return [row for row in found if row["created_by_id"] == "delivery-watchdog"]


async def test_threshold_comes_from_measured_ci_and_slow_takes_no_action(watch, clock):
    await _ci_samples(watch.state.db, "p", [2400] * 10, clock[0])
    await watch.complete("p", "t1", age=3600, rule="carried")
    report = await watch.scan()
    (delivery,) = report["deliveries"]
    assert (delivery["state"], delivery["reason"]) == ("progressing", "batch_queued")
    stored = await watch.rows(system_config, system_config.c.key == THRESHOLD_KEY + "p")
    assert '"stuck_after_seconds": 4800.0' in stored[0]["value"]
    row = await watch.blocker("t1")
    assert row["decision_inputs"]["threshold"]["p90_batch_ci_seconds"] == 2400
    # Past 2 x p90 a carried delivery is slow, never stuck: no handover, no incident.
    for _ in range(5):
        report = await watch.scan(advance=600)
    assert (report["deliveries"][0]["state"], report["deliveries"][0]["reason"]) == (
        "slow", "batch_queued")
    assert _takeovers(await watch.rows(tasks)) == []
    assert await watch.rows(escalations) == []


@pytest.mark.parametrize("rule", ["awaiting_delivery", "stranded_root"])
async def test_reobserved_generation_reuses_settled_projection(watch, clock, rule):
    await watch.complete("p", "t1", age=1900, rule=rule)
    await watch.scan()
    await watch.scan()
    original = await watch.blocker("t1")
    entry = watch.inventory.entries["p"].pop("t1")
    await watch.scan()
    settled = await watch.blocker("t1")
    assert settled["outcome"] == "satisfied"
    assert settled["resolved_at"] is not None and settled["cleared_at"] is not None

    await watch.restart()
    watch.inventory.entries["p"]["t1"] = entry
    delivery = (await watch.scan())["deliveries"][0]
    row = await watch.blocker("t1")
    assert row["id"] == original["id"]
    assert row["source_key"] == original["source_key"]
    assert row["created_at"] == original["created_at"]
    assert row["decision_inputs"]["first_completed_at"] == (
        original["decision_inputs"]["first_completed_at"])
    assert row["decision_inputs"]["redrive"] == original["decision_inputs"]["redrive"]
    assert delivery["age_seconds"] > original["decision_inputs"]["age_seconds"]
    assert row["outcome"] == "pending"
    assert row["resolved_at"] is None and row["cleared_at"] is None
    assert row["version"] > settled["version"]

    await watch.scan()
    assert (await watch.blocker("t1"))["outcome"] == "pending"
    assert len(_takeovers(await watch.rows(tasks))) == (1 if rule == "stranded_root" else 0)


async def test_stuck_generation_gets_one_takeover_and_one_incident_across_restart(watch, clock):
    await watch.complete("p", "t1", age=1900)
    first = (await watch.scan())["deliveries"][0]
    assert (first["state"], first["reason"], first["stuck_scans"]) == (
        "stuck", "publisher_unavailable", 1)
    assert first["redrive"] is None
    second = (await watch.scan())["deliveries"][0]
    (takeover,) = _takeovers(await watch.rows(tasks))
    assert second["redrive"]["task_id"] == takeover["id"]
    assert (second["state"], second["reason"]) == ("stuck", "publisher_unavailable")
    assert takeover["status"] == "READY" and takeover["title"] == "Land stuck delivery of t1"
    assert "origin/aq/t1" in takeover["description"]
    # A restarted daemon holds no memory: every later scan reads the durable projection.
    await watch.restart()
    third = (await watch.scan())["deliveries"][0]
    assert (third["state"], third["reason"]) == ("progressing", "fixer_unclaimed")
    assert len(_takeovers(await watch.rows(tasks))) == 1
    assert await watch.rows(escalations) == []
    # Still owed one threshold after the handover: exactly one incident and notice.
    for _ in range(2):
        report = await watch.scan(advance=1800)
    await watch.restart()
    report = await watch.scan(advance=1800)
    incident_id = report["deliveries"][0]["incident_id"]
    (incident,) = await watch.rows(escalations)
    assert incident["id"] == incident_id and incident["source_kind"] == (
        "integration_delivery_stuck")
    assert len(await watch.rows(messages, messages.c.thread_id == incident_id)) == 1
    assert len(_takeovers(await watch.rows(tasks))) == 1
    # Containment settles the projection, resolves the incident, records recovery.
    watch.inventory.entries["p"].clear()
    assert (await watch.scan())["deliveries"] == []
    row = await watch.blocker("t1")
    assert row["outcome"] == "satisfied"
    (incident,) = await watch.rows(escalations)
    assert incident["state"] == "resolved"
    assert len(await watch.rows(events, events.c.event_type == "delivery.recovered")) == 1


async def test_failed_takeover_is_repair_deferred_and_raises_its_incident(watch, clock):
    await watch.complete("p", "t1", age=1900)
    await watch.scan()
    await watch.scan()
    (takeover,) = _takeovers(await watch.rows(tasks))
    async with watch.state.db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == takeover["id"])
                           .values(status="FAILED"))
    delivery = (await watch.scan())["deliveries"][0]
    assert (delivery["state"], delivery["reason"]) == ("stuck", "repair_deferred")
    assert delivery["incident_id"]
    await watch.scan()
    assert len(await watch.rows(escalations)) == 1
    assert len(_takeovers(await watch.rows(tasks))) == 1


async def test_hold_is_never_bypassed(watch, clock):
    await watch.complete("p", "t1", age=1900)
    async with watch.state.db.immediate() as conn:
        await conn.execute(insert(task_labels).values(task_id="t1", label="hold:operator"))
    for _ in range(4):
        delivery = (await watch.scan(advance=1800))["deliveries"][0]
    assert (delivery["state"], delivery["reason"], delivery["stuck_scans"]) == (
        "held", "manual_hold", 0)
    assert (await watch.blocker("t1"))["decision_reason"] == "manual_hold"
    assert _takeovers(await watch.rows(tasks)) == []
    assert await watch.rows(escalations) == []


async def test_unavailable_git_resets_stuck_scans_and_never_hands_over(watch, clock):
    await watch.complete("p", "t1", age=1900)
    assert (await watch.scan())["deliveries"][0]["stuck_scans"] == 1
    watch.inventory.unavailable.add("p")
    report = await watch.scan()
    assert report["unavailable"] == ["p"] and report["deliveries"] == []
    row = await watch.blocker("t1")
    assert (row["outcome"], row["verification_state"], row["decision_reason"]) == (
        "pending", "unavailable", "target_unavailable")
    assert row["decision_inputs"]["stuck_scans"] == 0
    assert '"p"' in (await watch.rows(system_config, system_config.c.key == CURSOR_KEY)
                     )[0]["value"]
    watch.inventory.unavailable.clear()
    assert (await watch.scan())["deliveries"][0]["stuck_scans"] == 1
    assert _takeovers(await watch.rows(tasks)) == []


async def test_exact_reason_codes_and_unknown_evidence_never_counts(watch, clock):
    await watch.complete("p", "t-unknown", age=7200, rule="unknown")
    await watch.complete("p", "t-missing", age=7200, rule="unreconciled")
    await watch.complete("p", "t-young", age=60, rule="awaiting_delivery")
    original = delivery_truth.load_delivery_requests

    async def without_source(*args, **kwargs):
        found = await original(*args, **kwargs)
        found["t-missing"].has_recorded_source = False
        return found

    delivery_truth.load_delivery_requests = without_source
    try:
        report = await watch.scan()
        reasons = {d["task_id"]: (d["state"], d["reason"]) for d in report["deliveries"]}
        assert reasons == {
            "t-unknown": ("unknown", "delivery_observation_unavailable"),
            "t-missing": ("stuck", "missing_source_provenance"),
            "t-young": ("progressing", "candidate_ci_pending"),
        }
        assert (await watch.blocker("t-missing"))["decision_reason"] == (
            "missing_source_provenance")
        for _ in range(3):
            report = await watch.scan()
    finally:
        delivery_truth.load_delivery_requests = original
    reasons = {d["task_id"]: d["reason"] for d in report["deliveries"]}
    assert reasons["t-unknown"] == "delivery_observation_unavailable"
    assert reasons["t-missing"] == "fixer_unclaimed"
    assert [t["title"] for t in _takeovers(await watch.rows(tasks))] == [
        "Land stuck delivery of t-missing"]


async def test_disabled_project_is_reported_without_reading_git(watch, clock):
    await watch.project("q", mode="disabled")
    report = await watch.scan()
    assert report["train_disabled"] == ["q"]
    assert watch.inventory.calls == ["p"]


async def test_cursor_pages_projects_oldest_first(watch, clock):
    await watch.project("q")
    await watch.complete("p", "tp", age=10)
    await watch.complete("q", "tq-new", age=10)
    await watch.complete("q", "tq-old", age=20)
    assert (await watch.scan(page_size=1))["projects"] == ["p"]
    report = await watch.scan(page_size=1)
    assert report["projects"] == ["q"]
    assert [d["task_id"] for d in report["deliveries"]] == ["tq-old", "tq-new"]
    assert (await watch.scan(page_size=1))["projects"] == ["p"]


async def test_project_override_replaces_measured_threshold(watch, clock):
    await _ci_samples(watch.state.db, "p", [2400] * 10, clock[0])
    async with watch.state.db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_policy={"delivery_watchdog": {"stuck_after_seconds": 600}}))
    await watch.complete("p", "t1", age=700)
    await watch.scan()
    row = await watch.blocker("t1")
    assert row["decision_inputs"]["threshold"]["stuck_after_seconds"] == 600
    assert row["decision_inputs"]["delivery_state"] == "stuck"


def test_watchdog_policy_is_validated_and_omitted_at_default():
    with pytest.raises(ValueError):
        DeliveryWatchdogPolicy(min_seconds=0)
    assert DeliveryWatchdogPolicy().stuck_after_seconds is None


async def test_doctor_names_stuck_generation_reason_and_its_landing_task(watch, clock):
    from src.doctor.stall_checks import _delivery_stuck_findings

    await watch.complete("p", "t1", age=1900)
    await watch.complete("p", "t2", age=60, rule="carried")
    ctx = SimpleNamespace(db=watch.state.db)
    await watch.scan()
    (finding,) = await _delivery_stuck_findings(ctx, {"p"})
    assert (finding["kind"], finding["task_id"], finding["reason"]) == (
        "delivery_stuck", "t1", "publisher_unavailable")
    assert "aq wait explain --task t1" in finding["detail"]
    await watch.scan()
    (finding,) = await _delivery_stuck_findings(ctx, {"p"})
    (takeover,) = _takeovers(await watch.rows(tasks))
    assert finding["takeover_task_id"] == takeover["id"]
    assert await _delivery_stuck_findings(ctx, {"other"}) == []
