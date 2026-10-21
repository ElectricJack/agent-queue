"""Generation-based delivery latency: recording transitions and aggregating a cohort."""

import contextlib
import json
import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select, update

from src.api.metrics import build_metrics_router
from src.database import Database
from src.database.tables import (
    events,
    ingest_batch_members,
    ingest_batches,
    integration_batch_members,
    integration_batches,
    projects,
    repos,
    task_completion_records,
    task_proposals,
    tasks,
    wait_measurements,
    wait_records,
)
from src.integration.delivery_observer import DeliveryObserver
from src.metrics.delivery_latency import (
    EVENT_TYPE,
    TARGET_MIN_SECONDS,
    aggregate,
    delivery_target,
    observation_rows,
    quantile,
    union_seconds,
)
from src.models import Project
from tests.db_fixtures import lease_dsn

T = 1_800_000_000.0
DEV = "refs/heads/main"
UNTIL = T + 86_400.0
SINCE = UNTIL - 86_400.0


def row(tid, completed, *, cid=None, project="p", status="COMPLETED", work_outcome=None,
        commits='["abc"]', branch="aq/x", parent=None, created=None):
    return {"id": cid or f"c-{tid}", "task_id": tid, "completed_at": completed,
            "work_outcome": work_outcome, "commits": commits, "branch": branch,
            "project_id": project, "status": status,
            "created_at": completed if created is None else created,
            "parent_task_id": parent, "branch_name": branch, "archived": False}


def obs(state, observed, *, ref=DEV, exclusion=None):
    return {"state": state, "observed_at": observed, "recorded_at": observed,
            "target_ref": ref, "exclusion": exclusion}


def wait(key, tid, kind, start, end, *, origin="integration", outcome="satisfied",
         repair=False):
    return {"id": key, "owner_kind": "task", "owner_id": tid, "origin": origin,
            "condition_kind": kind, "outcome": outcome, "created_at": start,
            "resolved_at": end, "cleared_at": None, "tasks": {tid}, "repair": repair}


def make_inputs(rows, observations=None, *, waits=(), measurements=None, ci=None,
                anchors=None, instrumented=T - 86_400.0, batches=(), completions=None):
    if completions is None:
        completions = {}
        for item in rows:
            completions.setdefault(item["task_id"], []).append((item["completed_at"], item["id"]))
    return {
        "cohort": list(rows),
        "completions": completions,
        "observations": {key: list(value) for key, value in (observations or {}).items()},
        "instrumented_at": {"p": instrumented} if instrumented is not None else {},
        "dev_refs": {"p": DEV},
        "waits": list(waits),
        "measurements": measurements or {},
        "ci_seconds": ci or {},
        "batches": list(batches),
        "anchors": anchors or {},
        "project_ids": ["p"],
    }


def report(inputs, *, now=UNTIL, until=UNTIL):
    return aggregate(inputs, since=until - 86_400.0, until=until, now=now, window="24h",
                     project_id="p")


def by_task(result):
    return {g["task_id"]: g for g in result["generations"]}


# ---------------------------------------------------------------- arithmetic


def test_quantile_interpolates_and_answers_none_without_data():
    assert quantile([], 0.5) is None
    assert quantile([7.0], 0.9) == 7.0
    assert quantile([4, 1, 3, 2], 0.5) == 2.5
    assert quantile([1, 2, 3, 4], 0.9) == pytest.approx(3.7)


def test_union_counts_overlap_once_and_ignores_empty_spans():
    assert union_seconds([(0, 10), (5, 15), (20, 25), (3, 3), (9, 2)]) == 20
    assert union_seconds([]) == 0


def test_target_is_floor_without_ci_and_derived_from_p90_otherwise():
    floor = delivery_target([])
    assert floor["target_seconds"] == TARGET_MIN_SECONDS
    assert floor["source"] == "floor" and floor["p90_batch_ci_seconds"] is None
    derived = delivery_target([600.0] * 8 + [2_000.0, 3_000.0])
    assert derived["source"] == "derived" and derived["batch_ci_samples"] == 10
    assert derived["p90_batch_ci_seconds"] == pytest.approx(2_100)
    assert derived["target_seconds"] == pytest.approx(4_200)
    assert delivery_target([60.0])["target_seconds"] == TARGET_MIN_SECONDS


# --------------------------------------------------------------- aggregation


def test_no_samples_is_null_not_zero():
    result = report(make_inputs([]))
    assert result["sample_count"] == 0 and result["eligible_count"] == 0
    assert result["median_seconds"] is None and result["p90_seconds"] is None
    assert result["coverage"] is None and result["within_target_fraction"] is None
    assert result["oldest_pending_age_seconds"] is None
    assert all(s["median_seconds"] is None for s in result["segments"])
    assert result["target"]["source"] == "floor"


def test_sample_measures_from_task_creation_without_a_spec():
    rows = [row("a", T, created=T - 400)]
    result = report(make_inputs(rows, {("a", "c-a"): [obs("pending", T + 60),
                                                      obs("contained", T + 600)]},
                                anchors={"a": (T - 400, "task_created")}))
    generation = by_task(result)["a"]
    assert generation["status"] == "sample" and generation["anchor_kind"] == "task_created"
    assert generation["approval_to_dev_seconds"] == 1_000
    assert generation["completion_to_dev_seconds"] == 600
    assert result["median_seconds"] == 1_000
    assert result["completion_to_dev_median_seconds"] == 600
    assert result["metric"] == "approval_to_dev" and result["within_target_fraction"] == 1.0


def test_approval_anchor_subtracts_human_waits_after_approval_only():
    rows = [row("a", T)]
    human = wait("h", "a", "human", T - 6_000, T - 1_000, origin="agent")
    result = report(make_inputs(rows, {("a", "c-a"): [obs("contained", T + 1_000)]},
                                waits=[human], anchors={"a": (T - 5_000, "spec_approved")}))
    generation = by_task(result)["a"]
    assert generation["anchor_kind"] == "spec_approved" and generation["anchor_at"] == T - 5_000
    assert generation["human_wait_seconds"] == 4_000
    assert generation["approval_to_dev_seconds"] == 2_000
    assert generation["completion_to_dev_seconds"] == 1_000


def test_anchor_after_completion_is_capped_at_completion():
    rows = [row("a", T)]
    result = report(make_inputs(rows, {("a", "c-a"): [obs("contained", T + 300)]},
                                anchors={"a": (T + 100, "spec_approved")}))
    generation = by_task(result)["a"]
    assert generation["anchor_at"] == T and generation["approval_to_dev_seconds"] == 300


def test_late_sample_counts_in_its_completion_cohort():
    rows = [row("a", UNTIL - 100)]
    result = report(make_inputs(rows, {("a", "c-a"): [obs("contained", UNTIL + 500)]}),
                    now=UNTIL + 1_000)
    assert result["sample_count"] == 1 and result["late_sample_count"] == 1
    assert by_task(result)["a"]["late"] is True


def test_reopened_task_counts_each_generation_once():
    first, second = row("a", T, cid="g1"), row("a", T + 1_000, cid="g2")
    result = report(make_inputs([first, second], {
        ("a", "g1"): [obs("pending", T + 10)],
        ("a", "g2"): [obs("pending", T + 1_010), obs("contained", T + 1_500),
                      obs("contained", T + 9_000)],
    }))
    states = {g["generation_id"]: g for g in result["generations"]}
    assert states["g1"]["exclusion"] == "superseded"
    assert states["g2"]["status"] == "sample"
    assert states["g2"]["on_dev_at"] == T + 1_500
    assert result["sample_count"] == 1 and result["eligible_count"] == 1
    assert {"reason": "superseded", "count": 1} in result["excluded"]


def test_clock_skew_is_capped_at_zero_and_flagged():
    rows = [row("a", T)]
    result = report(make_inputs(rows, {("a", "c-a"): [obs("contained", T - 30)]}))
    generation = by_task(result)["a"]
    assert generation["completion_to_dev_seconds"] == 0 and generation["clock_skew"] is True
    assert result["clock_skew_count"] == 1


def test_exclusions_leave_the_eligible_denominator():
    rows = [
        row("other", T, cid="c1"),
        row("nochange", T, cid="c2"),
        row("empty", T, cid="c3", commits="[]", branch=None),
        row("noop", T, cid="c4", work_outcome="no-op"),
        row("cancelled", T, cid="c5", status="CANCELLED"),
        row("settled", T, cid="c6"),
        row("landed", T, cid="c7"),
    ]
    result = report(make_inputs(rows, {
        ("other", "c1"): [obs("pending", T + 5, exclusion="delivered_by_other_means")],
        ("nochange", "c2"): [obs("no_change", T + 5)],
        ("cancelled", "c5"): [obs("pending", T + 5)],
        ("settled", "c6"): [obs("settled", T + 5)],
        ("landed", "c7"): [obs("contained", T + 50)],
    }))
    excluded = {item["reason"]: item["count"] for item in result["excluded"]}
    assert excluded == {"delivered_by_other_means": 1, "code_free": 3, "abandoned": 1,
                        "superseded": 0, "settled": 1}
    assert result["eligible_count"] == 1 and result["coverage"] == 1.0


def test_ref_rewrite_keeps_the_first_sighting_and_flags_the_blocker():
    rows = [row("a", T)]
    result = report(make_inputs(rows, {("a", "c-a"): [obs("contained", T + 100),
                                                      obs("pending", T + 5_000)]}))
    generation = by_task(result)["a"]
    assert generation["status"] == "sample" and generation["on_dev_at"] == T + 100
    assert generation["current_blocker"] is True and generation["latest_state"] == "pending"
    assert result["current_blocker_count"] == 1


def test_pending_never_enters_percentiles_but_is_counted():
    now = T + 10_000
    rows = [row("fast", T), row("medium", T), row("stuck", T), row("slow", T),
            row("young", now - 60)]
    result = report(make_inputs(rows, {
        ("fast", "c-fast"): [obs("contained", T + 100)],
        ("medium", "c-medium"): [obs("contained", T + 300)],
        ("stuck", "c-stuck"): [obs("pending", T + 10)],
        ("slow", "c-slow"): [obs("pending", T + 10)],
        ("young", "c-young"): [obs("pending", now - 50)],
    }, waits=[wait("live", "slow", "batch", T + 20, None, outcome="pending")]), now=now)
    assert result["percentiles_exclude_pending"] is True
    assert result["median_seconds"] == 200 and result["sample_count"] == 2
    assert result["pending_count"] == 3 and result["eligible_count"] == 5
    assert result["coverage"] == pytest.approx(0.4)
    assert result["oldest_pending_age_seconds"] == 10_000
    assert (result["slow_count"], result["stuck_count"]) == (1, 1)
    generations = by_task(result)
    assert generations["stuck"]["watchdog"] == "stuck"
    assert generations["slow"]["watchdog"] == "slow"
    assert "watchdog" not in generations["young"]
    assert [g["status"] for g in result["generations"][:3]] == ["pending"] * 3


def test_overlapping_segments_are_not_forced_to_sum():
    rows = [row("a", T)]
    linked = [
        wait("batch", "a", "batch", T, T + 300),
        wait("ci", "a", "job", T + 200, T + 500),
        wait("slot", "a", "resource", T + 100, T + 150),
        wait("agent", "a", "resource", T, T + 900, origin="agent"),
        wait("fix", "a", "batch", T + 600, T + 700, repair=True),
    ]
    result = report(make_inputs(rows, {("a", "c-a"): [obs("contained", T + 1_000)]},
                                waits=linked))
    generation = by_task(result)["a"]
    assert generation["segments"] == {"train_wait": 300, "ci_time": 300, "slot_wait": 50,
                                      "repair": 100}
    assert generation["residual_seconds"] == 1_000 - 500 - 100
    segments = {s["segment"]: s for s in result["segments"]}
    assert segments["ci_time"]["median_seconds"] == 300 and segments["ci_time"]["generations"] == 1
    assert segments["author_merge"]["generations"] == 0
    assert segments["author_merge"]["median_seconds"] == 0


def test_rebuild_measurements_derive_cache_hit_rate():
    rows = [row("a", T)]
    measured = {"ci": [{"wait_id": "ci", "waited_seconds": 300, "setup_seconds": 5,
                        "rebuild_seconds": 0}],
                "batch": [{"wait_id": "batch", "waited_seconds": 300, "setup_seconds": 7,
                           "rebuild_seconds": 40}]}
    result = report(make_inputs(rows, {("a", "c-a"): [obs("contained", T + 1_000)]},
                                waits=[wait("ci", "a", "job", T, T + 300),
                                       wait("batch", "a", "batch", T, T + 300)],
                                measurements=measured))
    generation = by_task(result)["a"]
    assert generation["rebuild_seconds"] == 40 and generation["setup_seconds"] == 12
    assert generation["cache_hit_rate"] == 0.5
    assert result["rebuild_seconds_total"] == 40 and result["cache_hit_rate"] == 0.5


def test_completions_before_instrumentation_are_censored_not_samples():
    start = T + 1_000
    rows = [row("old-landed", T), row("old-unseen", T), row("new-unseen", T + 2_000),
            row("old-pending", T)]
    result = report(make_inputs(rows, {
        ("old-landed", "c-old-landed"): [obs("contained", T + 1_100)],
        ("old-pending", "c-old-pending"): [obs("pending", T + 1_100),
                                           obs("contained", T + 1_400)],
    }, instrumented=start), now=T + 3_000)
    generations = by_task(result)
    assert generations["old-landed"]["status"] == "left_censored"
    assert generations["old-unseen"]["status"] == "historical_unknown"
    assert generations["new-unseen"]["status"] == "unknown"
    assert generations["old-pending"]["status"] == "sample"
    assert result["sample_count"] == 1 and result["left_censored_count"] == 1
    assert result["historical_unknown_count"] == 1 and result["unknown_count"] == 1
    assert result["unknown_fraction"] == pytest.approx(2 / 4)
    histogram = {b["bucket"]: b["count"] for b in result["historical_age_histogram"]}
    assert histogram["<1h"] == 1


def test_parent_ref_sightings_report_parent_delay():
    rows = [row("a", T)]
    result = report(make_inputs(rows, {("a", "c-a"): [
        obs("contained", T + 200, ref="refs/heads/aq/epic"), obs("contained", T + 900)]}))
    generation = by_task(result)["a"]
    assert generation["parent_ref"] == "refs/heads/aq/epic" and generation["parent_at"] == T + 200
    assert result["parent_delay_median_seconds"] == 200
    assert generation["on_dev_at"] == T + 900


def test_batches_per_day_sum_members_and_red_rounds():
    day = int(T // 86_400) * 86_400
    result = report(make_inputs([], batches=[
        {"id": "b1", "project_id": "p", "created_at": day + 10, "members": 3,
         "repair_attempt_count": 2},
        {"id": "b2", "project_id": "p", "created_at": day + 20, "members": 1,
         "repair_attempt_count": 0},
    ]))
    assert result["batches_per_day"] == [{"day": day, "batches": 2, "members": 4,
                                          "red_rounds": 2}]


# ----------------------------------------------------------------- recording


def evidence(task_id, state, *, completion_id="c1", legacy=None, project="p", repo="r",
             ref=DEV, exclusion=None):
    request = SimpleNamespace(completion_id=completion_id, legacy_generation=legacy,
                              completed_at=T, project_id=project, repository_id=repo,
                              target_ref=ref)
    return SimpleNamespace(request=request, state=state, reason="test", target_oid="t" * 40,
                           source_oid="s" * 40, lifecycle_exclusion=exclusion)


def view(found, *, error=None, observed=T + 60):
    snapshot = SimpleNamespace(project_id="p", repository_id="r", target_ref=DEV,
                               error=error, observed_at=observed)
    return SimpleNamespace(evidence=found, snapshots=(snapshot,))


def test_observation_rows_skip_unknown_and_failed_fetches():
    rows = observation_rows(view({
        "a": evidence("a", "pending"),
        "b": evidence("b", "unknown"),
        "c": evidence("c", "contained", completion_id=None, legacy="v3"),
        "d": evidence("d", "contained", completion_id=None),
        "e": evidence("e", "contained", ref="refs/heads/other"),
    }), now=T + 61)
    assert [key for key, _mark, _row in rows] == [("a", "c1", DEV), ("c", "legacy:v3", DEV)]
    payload = json.loads(rows[0][2]["payload"])
    assert payload["observed_at"] == T + 60 and payload["collector_at"] == T + 61
    assert rows[0][2]["event_type"] == EVENT_TYPE
    assert observation_rows(view({"a": evidence("a", "pending")}, error="boom"), now=T) == []


# ------------------------------------------------------------------ database


@pytest.fixture
async def db():
    database = Database(lease_dsn("delivery-latency"))
    await database.initialize()
    await database.create_project(Project(id="p", name="P"))
    async with database._engine.begin() as conn:
        await conn.execute(insert(repos).values(id="r", project_id="p", url="file:///r",
                                                checkout_base_path="/tmp/r"))
        await conn.execute(update(projects).where(projects.c.id == "p")
                           .values(integration_repository_id="r"))
    yield database
    await database.close()


async def stored(db):
    async with db._engine.connect() as conn:
        rows = await conn.execute(select(events.c.payload).where(
            events.c.event_type == EVENT_TYPE).order_by(events.c.id))
        return [json.loads(raw)["state"] for raw, in rows.all()]


async def test_recorder_writes_transitions_only_across_restarts(db):
    assert await db.record_delivery_observations(view({"a": evidence("a", "pending")})) == 1
    assert await db.record_delivery_observations(view({"a": evidence("a", "pending")})) == 0
    # A restart forgets the in-memory marks and reloads them from the events.
    db.__dict__.pop("_delivery_marks")
    db.__dict__.pop("_delivery_marks_loaded")
    assert await db.record_delivery_observations(view({"a": evidence("a", "pending")})) == 0
    assert await db.record_delivery_observations(view({"a": evidence("a", "contained")})) == 1
    assert await stored(db) == ["pending", "contained"]


async def test_snapshot_reads_cohort_waits_and_spec_anchor(db):
    now = time.time()
    completed = now - 3_000
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_proposals).values(
            id="prop", project_id="p", source="spec", payload="{}", created_at=now - 9_000,
            updated_at=now - 9_000))
        await conn.execute(insert(ingest_batches).values(
            batch_id="prop", project_id="p", source_review_id="missing-review",
            source_revision=1, source_content_sha256="0" * 64, decision_id="missing-decision",
            lineage_kind="none", idempotency_key="prop", structure_revision=1,
            structure_digest="d", created_at=now - 8_000, updated_at=now - 8_000))
        for tid, parent, created in (("epic", None, now - 9_000), ("leaf", "epic", now - 7_000)):
            await conn.execute(insert(tasks).values(
                id=tid, project_id="p", title=tid, description="", created_at=created,
                updated_at=created, parent_task_id=parent, status="COMPLETED",
                branch_name=f"aq/{tid}", legacy_completion_id=f"legacy-{tid}"))
        await conn.execute(insert(ingest_batch_members).values(
            batch_id="prop", task_id="epic", member_key="epic", role="container",
            section_anchor="s", primary_outcome="o", created_revision=1))
        await conn.execute(insert(task_completion_records).values(
            id="c1", task_id="leaf", outcome="pass", completed_at=completed,
            branch="aq/leaf", commits='["abc"]'))
        await conn.execute(insert(integration_batches).values(
            id="b1", project_id="p", repository_id="r", request_id="b1-request",
            target_ref=DEV, lifecycle="sealing", source_manifest_digest="digest",
            base_sha="0" * 40, integration_branch="refs/heads/aq/batches/b1",
            policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending",
            created_at=completed + 50, updated_at=completed + 50, repair_attempt_count=2))
        await conn.execute(insert(integration_batch_members).values(
            batch_id="b1", ordinal=0, task_id="leaf", repository_id="r", source_sha="3" * 40,
            source_base_sha="0" * 40, reviewed_head_sha="1" * 40, reviewed_tree_sha="2" * 40,
            review_evidence={}))
        for key, owner, kind, start, end in (("w-ci", "leaf", "job", 100, 700),
                                             ("w-train", "b1", "batch", 0, 400)):
            await conn.execute(insert(wait_records).values(
                id=key, project_id="p", owner_kind="task", owner_id=owner,
                origin="integration", condition_kind=kind, condition={"kind": kind},
                source_key=key, source_generation=1, source_watermark_seq=0,
                verification_state="verified", evidence_ref="test", verified_at=completed,
                created_at=completed + start, resolved_at=completed + end, outcome="satisfied",
                sleep_decision="in_place", decision_inputs={}, request_hash=key))
        await conn.execute(insert(wait_measurements).values(
            id="m1", project_id="p", condition_kind="job", source_class="integration",
            wait_id="w-ci", decision="in_place", waited_seconds=1_200.0, hibernated=False,
            evicted=False, rebuild_seconds=0.0, recorded_at=completed + 700))

    leaf = {"leaf": evidence("leaf", "pending")}
    assert await db.record_delivery_observations(view(leaf, observed=completed + 10)) == 1
    leaf = {"leaf": evidence("leaf", "contained")}
    assert await db.record_delivery_observations(view(leaf, observed=completed + 1_000)) == 1

    result = await db.metrics_delivery_latency(window="24h", project_id="p")
    assert result["sample_count"] == 1
    generation = by_task(result)["leaf"]
    assert generation["dev_ref"] == DEV and generation["anchor_kind"] == "spec_ingested"
    assert generation["anchor_at"] == pytest.approx(now - 8_000)
    assert generation["completion_to_dev_seconds"] == pytest.approx(1_000)
    assert generation["approval_to_dev_seconds"] == pytest.approx(completed + 1_000 - now + 8_000)
    assert generation["segments"] == {"ci_time": pytest.approx(600),
                                      "train_wait": pytest.approx(400)}
    assert generation["cache_hit_rate"] == 1.0
    assert result["target"]["source"] == "derived"
    assert result["target"]["target_seconds"] == pytest.approx(2_400)
    assert result["coverage"] == 1.0
    assert [(day["batches"], day["members"], day["red_rounds"])
            for day in result["batches_per_day"]] == [(1, 1, 2)]

    async with AsyncClient(transport=ASGITransport(app=_app(db)), base_url="http://t") as ac:
        response = await ac.get("/api/metrics/delivery-latency",
                                params={"window": "7d", "project_id": "p"})
        assert response.status_code == 200
        body = response.json()
        assert body["metric"] == "approval_to_dev" and body["sample_count"] == 1
        assert body["generations"][0]["segments"]["ci_time"] == pytest.approx(600)
        assert (await ac.get("/api/metrics/delivery-latency",
                             params={"window": "1h"})).status_code == 422


def _app(db):
    app = FastAPI()
    app.include_router(build_metrics_router(db=db))
    return app


# ------------------------------------------------------------------ observer


class _Engine:
    @contextlib.asynccontextmanager
    async def connect(self):
        yield None


async def test_recorder_failure_never_breaks_observation(tmp_path, caplog):
    async def boom(view):
        raise RuntimeError("telemetry down")

    async def no_targets(conn, ids):
        return {}

    db = SimpleNamespace(_engine=_Engine(), record_delivery_observations=boom)
    observer = DeliveryObserver(db, git=None, data_dir=tmp_path)
    with caplog.at_level(logging.WARNING):
        result = await observer.observe(["a"], target_loader=no_targets)
    assert result.evidence == {}
    assert "delivery observation telemetry failed" in caplog.text
