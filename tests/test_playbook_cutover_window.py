"""Package 7 commit 3 — the rollback observation window, measured from source.

Child plan §3.5 / §5.3 T-11, T-12
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md``).

Every §3.5 measure has a real evidence source here, and every source is
fail-closed: an unreadable one blocks the window rather than being rendered
as satisfied.  The T-12 parametrisation in
``tests/fixtures/playbooks/cutover/window-measures.json`` plants one failing
source per measure and asserts ``playbook_cutover_window_close`` refuses and
names that measure — after a *passing* status call, so the refusal proves the
close recomputed rather than trusted.
"""

from __future__ import annotations

import json
import pathlib
import time
from types import SimpleNamespace

import pytest

from src.playbooks.cutover_window import (
    DASHBOARD_TTI_GATE_MS,
    GRAPH_LATENCY_GATE_MS,
    MEASURE_NAMES,
    WINDOW_MIN_SECONDS,
    WINDOW_MIN_V2_RUNS,
    WindowEvidence,
    evaluate_window,
)

FIXTURES = pathlib.Path("tests/fixtures/playbooks/cutover")
PARITY_REPORT = pathlib.Path("tests/fixtures/playbooks/v2/parity-report.json")
NOW = 1_800_000_000.0
SWITCHED_AT = NOW - 80 * 3600.0


# ---------------------------------------------------------------------------
# Evidence builders
# ---------------------------------------------------------------------------


def _parity_report(*, unexplained_field: str | None = None) -> dict:
    """The committed record, optionally with one finding turned unexplained."""
    report = json.loads(PARITY_REPORT.read_text(encoding="utf-8"))
    if unexplained_field is not None:
        clean = next(event for event in report["events"] if not event["findings"])
        clean["findings"] = [
            {"field": unexplained_field, "classification": "unexplained", "rationale_id": None}
        ]
        report["identical"] -= 1
        report["unexplained"] += 1
    return report


def _activations(parity_sha: str) -> list[dict]:
    rows = []
    for playbook_id, sha in (
        ("default-pipeline", parity_sha),
        ("memory-consolidation", "sha256:" + "a" * 64),
        ("coding-reflection", "sha256:" + "b" * 64),
    ):
        rows.append(
            {
                "playbook_id": playbook_id,
                "scope": "system",
                "scope_identifier": "",
                "active_artifact_sha256": sha,
                "artifact_sha256": sha,
                "enabled": True,
                "health": "ready",
                "size_bytes": 4096 if playbook_id == "default-pipeline" else 1024,
            }
        )
    return rows


def _healthy_step_counts() -> list[dict]:
    return [
        {"step_kind": "llm", "receipt_kind": "step", "outcome": "success",
         "error_code": None, "count": 100},
        {"step_kind": "agent_task", "receipt_kind": "step", "outcome": "success",
         "error_code": None, "count": 40},
        {"step_kind": "command", "receipt_kind": "step", "outcome": "success",
         "error_code": None, "count": 300},
    ]


def _healthy_evidence(**overrides) -> WindowEvidence:
    parity = _parity_report()
    fields = {
        "now": NOW,
        "switched_at": SWITCHED_AT,
        "parity": parity,
        "enabled_activations": _activations(parity["artifact_sha256"]),
        "v2_runs_by_playbook": {
            "default-pipeline": 150, "memory-consolidation": 30, "coding-reflection": 25
        },
        "denials": [],
        "conflicts": [],
        "dispatch_latencies_ms": [400.0] * 20,
        "resume_latencies_ms": [1200.0] * 10,
        "v1_baseline": {"sample_size": 40, "dispatch_p95": 0.64, "resume_p95": 1.0},
        "step_counts": _healthy_step_counts(),
        "agent_task_orphans": [],
        "agent_task_cancellations": [],
        "graph_latencies_ms": [120.0, 130.0, 140.0, 150.0, 160.0],
        "graph_target": "default-pipeline",
        "dashboard_tti": {"ms": 900.0, "recorded_at": NOW - 3600.0, "actor": "operator"},
        "pending": {"count": 0, "oldest_received_at": None},
        "active_v1_runs": 0,
        "rehearsal": {"at": NOW - 3600.0, "playbooks": [
            "default-pipeline", "memory-consolidation", "coding-reflection"
        ]},
        "errors": {},
    }
    fields.update(overrides)
    return WindowEvidence(**fields)


# ---------------------------------------------------------------------------
# The pure evaluator
# ---------------------------------------------------------------------------


def test_healthy_evidence_passes_every_measure_and_every_window_condition():
    verdict = evaluate_window(_healthy_evidence())

    assert verdict.blocking_reasons == []
    assert [row["measure"] for row in verdict.measures] == list(range(1, 17))
    assert all(row["pass"] for row in verdict.measures)
    assert all(row["observed"] is not None for row in verdict.measures)
    assert all(row["observed_at"] == NOW for row in verdict.measures)
    assert verdict.window["wall_clock_ok"] is True
    assert verdict.window["coverage_ok"] is True
    assert verdict.window["volume_ok"] is True
    assert verdict.window["v2_run_count"] == 205
    assert verdict.window["since"] == SWITCHED_AT and verdict.window["until"] == NOW


def test_every_measure_names_its_source_and_gate():
    verdict = evaluate_window(_healthy_evidence())
    for row in verdict.measures:
        assert row["name"] == MEASURE_NAMES[row["measure"]]
        assert row["source"], row
        assert row["gate"], row
        assert "not measured" not in row["source"]


def test_window_refuses_on_wall_clock_alone():
    verdict = evaluate_window(_healthy_evidence(switched_at=NOW - 3600.0))
    assert any(
        f"{WINDOW_MIN_SECONDS:.0f}s required" in reason for reason in verdict.blocking_reasons
    )


def test_window_close_refuses_without_coverage():
    """T-11: wall clock and volume satisfied, one enabled playbook never ran."""
    verdict = evaluate_window(
        _healthy_evidence(
            v2_runs_by_playbook={"default-pipeline": 200, "memory-consolidation": 30}
        )
    )
    assert verdict.window["coverage_ok"] is False
    assert verdict.window["coverage_missing"] == ["coding-reflection"]
    assert (
        "coverage: playbook 'coding-reflection' has dispatched 0 v2 runs since the switch"
        in verdict.blocking_reasons
    )
    assert verdict.window["volume_ok"] is True


def test_window_refuses_below_the_volume_floor():
    verdict = evaluate_window(
        _healthy_evidence(
            v2_runs_by_playbook={
                "default-pipeline": 5, "memory-consolidation": 5, "coding-reflection": 5
            }
        )
    )
    assert verdict.window["volume_ok"] is False
    assert any(
        f"{WINDOW_MIN_V2_RUNS} required" in reason for reason in verdict.blocking_reasons
    )


def test_window_has_not_started_without_a_switch_event():
    verdict = evaluate_window(_healthy_evidence(switched_at=None))
    assert "no switched_to_v2 event — the window has not started" in verdict.blocking_reasons
    assert verdict.window["since"] is None


def test_an_unreadable_source_blocks_every_measure_it_feeds():
    """Fail-closed: a source that could not be read is never rendered as fine."""
    verdict = evaluate_window(
        _healthy_evidence(step_counts=None, errors={"receipts": "table unreadable"})
    )
    failing = {row["measure"] for row in verdict.measures if not row["pass"]}
    assert failing == {5, 8, 9, 10, 11}
    for row in verdict.measures:
        if row["measure"] in failing:
            assert "evidence unreadable" in row["blocking"]
            assert "table unreadable" in row["blocking"]
    assert verdict.evidence_errors == ["receipts: table unreadable"]


def test_parity_report_must_bind_to_the_live_artifact():
    """A stale report — recorded against bytes nobody activates — is not
    evidence; measures 1-3 ask for it to be re-run and re-committed."""
    parity = _parity_report()
    stale = _activations("sha256:" + "f" * 64)
    verdict = evaluate_window(_healthy_evidence(enabled_activations=stale))
    for measure in (1, 2, 3):
        row = verdict.measures[measure - 1]
        assert row["pass"] is False
        assert "re-run the parity suite" in row["blocking"], row
    assert parity["unexplained"] == 0  # the counters were clean; binding failed


def test_shadow_denials_do_not_count_against_measure_4():
    verdict = evaluate_window(
        _healthy_evidence(
            denials=[{"command": "ensure_task", "profile_id": "reviewer", "shadow": True}]
        )
    )
    row = verdict.measures[3]
    assert row["pass"] is True
    assert row["observed"]["total"] == 0
    assert row["observed"]["shadow"] == 1


def test_measure_6_needs_the_v1_baseline():
    verdict = evaluate_window(_healthy_evidence(v1_baseline=None))
    row = verdict.measures[5]
    assert row["pass"] is False
    assert "no v1 baseline" in row["blocking"]


def test_measure_6_is_anchored_to_the_baseline_not_just_the_absolute_gate():
    """900 ms clears the 1000 ms ceiling but not 1.25 x a 640 ms baseline."""
    verdict = evaluate_window(_healthy_evidence(dispatch_latencies_ms=[900.0] * 10))
    row = verdict.measures[5]
    assert row["pass"] is False
    assert "1.25 x baseline 640ms" in row["blocking"]


def test_zero_llm_steps_is_not_a_zero_failure_rate():
    verdict = evaluate_window(
        _healthy_evidence(
            step_counts=[
                {"step_kind": "command", "receipt_kind": "step", "outcome": "success",
                 "error_code": None, "count": 10}
            ]
        )
    )
    for measure in (8, 9, 10):
        row = verdict.measures[measure - 1]
        assert row["pass"] is False, row
        assert "no " in row["blocking"]


def test_measure_11_is_reported_without_a_gate():
    counts = _healthy_step_counts() + [
        {"step_kind": "agent_task", "receipt_kind": "step", "outcome": "cancelled",
         "error_code": "cancelled", "count": 4}
    ]
    cancellations = [
        {"run_id": "r-b", "step_id": "review", "started_at": NOW - 50.0, "cancelled_at": NOW - 40.0},
        {"run_id": "r-a", "step_id": "review", "started_at": NOW - 30.0, "cancelled_at": NOW - 20.0},
        {"run_id": "r-a", "step_id": "verify", "started_at": NOW - 10.0, "cancelled_at": NOW - 5.0},
        {"run_id": "r-c", "step_id": "review", "started_at": NOW - 9.0, "cancelled_at": NOW - 1.0},
    ]
    verdict = evaluate_window(
        _healthy_evidence(step_counts=counts, agent_task_cancellations=cancellations)
    )
    row = verdict.measures[10]
    assert row["pass"] is True
    assert row["observed"] == {
        "cancelled": 4, "agent_task_steps": 44, "rate": 4 / 44, "runs": ["r-a", "r-b", "r-c"]
    }
    assert "no gate" in row["gate"]


def test_measure_13_reports_the_recorded_review_and_its_timestamp():
    verdict = evaluate_window(_healthy_evidence())
    row = verdict.measures[12]
    assert row["observed"] == {"ms": 900.0, "recorded_at": NOW - 3600.0, "actor": "operator"}
    assert f"{DASHBOARD_TTI_GATE_MS:.0f}" in row["gate"]


def test_measure_12_names_the_artifact_it_probed():
    verdict = evaluate_window(_healthy_evidence())
    row = verdict.measures[11]
    assert row["observed"]["target"] == "default-pipeline"
    assert row["observed"]["samples"] == 5
    assert f"{GRAPH_LATENCY_GATE_MS}" in row["gate"]


def test_measure_15_passes_with_no_pending_events():
    verdict = evaluate_window(_healthy_evidence())
    assert verdict.measures[14]["pass"] is True
    assert verdict.measures[14]["observed"]["max_age_seconds"] == 0.0


# ---------------------------------------------------------------------------
# The commands — evidence collected from the database, verdict recomputed
# ---------------------------------------------------------------------------


class _Unreadable(Exception):
    pass


class _WindowDB:
    """Every read the window commands make, with one attribute per source."""

    def __init__(self, parity_sha: str):
        self.events: list[dict] = [
            {"event_id": "e0", "kind": "v1_admission_closed", "at": SWITCHED_AT - 100,
             "actor": "op", "reason": "draining", "detail": {}},
            {"event_id": "e2", "kind": "switched_to_v2", "at": SWITCHED_AT, "actor": "op",
             "reason": "cutting over", "detail": {"from": "v1", "to": "v2", "v1_baseline": {"sample_size": 40, "dispatch_p95": 0.64, "resume_p95": 1.0}}},
            {"event_id": "e3", "kind": "window_coverage_rehearsal", "at": NOW - 3600.0,
             "actor": "op", "reason": "rehearsal",
             "detail": {"playbooks": ["default-pipeline", "memory-consolidation",
                                      "coding-reflection"],
                        "dashboard_tti_ms": 900.0}},
        ]
        self.v1_runs: list = []
        self.activations = _activations(parity_sha)
        self.runs_by_playbook = {
            "default-pipeline": 150, "memory-consolidation": 30, "coding-reflection": 25
        }
        self.denials: list[dict] = []
        self.conflicts: list[dict] = []
        self.dispatch_latencies_ms = [400.0] * 20
        self.resume_latencies_ms = [1200.0] * 10
        self.step_counts: list[dict] | dict = _healthy_step_counts()
        self.orphans: list[dict] = []
        self.cancellations: list[dict] | dict = []
        self.pending = {"count": 0, "oldest_received_at": None}
        self.calls: list[str] = []

    # -- cutover audit --------------------------------------------------
    async def latest_playbook_cutover_event(self, kind):
        matches = [e for e in self.events if e["kind"] == kind]
        return max(matches, key=lambda e: e["at"]) if matches else None

    async def list_playbook_cutover_events(self, kind=None, limit=500):
        rows = [e for e in self.events if kind is None or e["kind"] == kind]
        return sorted(rows, key=lambda e: e["at"])[:limit]

    async def append_playbook_cutover_event(self, *, kind, actor, reason, detail=None, at=None):
        event = {"event_id": f"e{len(self.events)}", "kind": kind,
                 "at": at if at is not None else time.time(), "actor": actor,
                 "reason": reason, "detail": dict(detail or {})}
        self.events.append(event)
        return event

    # -- V1 drain -------------------------------------------------------
    async def list_playbook_runs(self, playbook_id=None, status=None, limit=50):
        return [r for r in self.v1_runs if status is None or r.status == status][:limit]

    # -- window sources -------------------------------------------------
    async def list_playbook_activations_with_artifacts(self, *, enabled_only=False):
        return [dict(row) for row in self.activations if row["enabled"] or not enabled_only]

    async def count_v2_runs_by_playbook(self, since):
        self.calls.append(f"count_v2_runs_by_playbook:{since}")
        return dict(self.runs_by_playbook)

    async def get_recent_events(self, limit=50, *, event_type=None, since=None, **_):
        self.calls.append(f"get_recent_events:{event_type}:{since}")
        source = {"capability.denied": self.denials,
                  "playbook.snapshot_conflict": self.conflicts}[event_type]
        return [{"event_type": event_type, "timestamp": NOW - 10,
                 "payload": json.dumps(row)} for row in source][:limit]

    async def v2_dispatch_latencies_ms(self, since, *, limit=5000):
        return list(self.dispatch_latencies_ms)

    async def wait_resume_latencies_ms(self, since, *, limit=5000):
        return list(self.resume_latencies_ms)

    async def count_step_receipts_since(self, since):
        self.calls.append(f"count_step_receipts_since:{since}")
        if isinstance(self.step_counts, dict):
            raise _Unreadable(self.step_counts["error"])
        return [dict(row) for row in self.step_counts]

    async def agent_task_wait_orphans(self, now):
        return [dict(row) for row in self.orphans]

    async def agent_task_cancellations_since(self, since, *, limit=1000):
        if isinstance(self.cancellations, dict):
            raise _Unreadable(self.cancellations["error"])
        return [dict(row) for row in self.cancellations]

    async def pending_event_summary(self, *, reasons=None):
        summary = dict(self.pending)
        oldest = summary.get("oldest_received_at")
        if oldest == "recent":
            summary["oldest_received_at"] = NOW - 60.0
        elif oldest == "stale":
            summary["oldest_received_at"] = NOW - 90_000.0
        return summary


def _config(*, enabled=True, v2_engine=True, v1_admission="closed", v2_api=True):
    from src.config import PlaybooksConfig

    return SimpleNamespace(
        playbooks=PlaybooksConfig(
            enabled=enabled, v2_engine=v2_engine, v1_admission=v1_admission, v2_api=v2_api
        )
    )


def _handler(db, config=None, *, graph_latencies_ms=(120.0, 130.0, 140.0, 150.0, 160.0),
             parity_path=PARITY_REPORT, clock=lambda: NOW):
    from src.commands.playbook_cutover_commands import PlaybookCutoverCommandsMixin

    class _Handler(PlaybookCutoverCommandsMixin):
        def __init__(self):
            self.db = db
            self.config = config or _config()
            self.orchestrator = SimpleNamespace(playbook_manager=None, bus=None)
            self.probed: list[str] = []

        def _cutover_clock(self):
            return clock()

        def _cutover_parity_path(self):
            return pathlib.Path(parity_path)

        async def _cutover_probe_graph_latency_ms(self, playbook_id, *, samples=5):
            self.probed.append(playbook_id)
            if isinstance(graph_latencies_ms, Exception):
                raise graph_latencies_ms
            return list(graph_latencies_ms)

    return _Handler()


def _parity_sha() -> str:
    return _parity_report()["artifact_sha256"]


@pytest.mark.asyncio
async def test_window_status_measures_every_row_from_source():
    db = _WindowDB(_parity_sha())
    handler = _handler(db)

    status = await handler._cmd_playbook_cutover_window_status({})

    assert status["success"] is True
    assert status["blocking_reasons"] == []
    assert status["can_close"] is True
    assert [row["measure"] for row in status["measures"]] == list(range(1, 17))
    assert all(row["pass"] for row in status["measures"])
    assert status["window"]["since"] == SWITCHED_AT
    assert status["window"]["v2_run_count"] == 205
    assert status["window"]["rehearsal_at"] == NOW - 3600.0
    assert status["evidence_errors"] == []
    # Every windowed read was bounded by the durable switch timestamp.
    assert f"count_v2_runs_by_playbook:{SWITCHED_AT}" in db.calls
    assert f"get_recent_events:capability.denied:{SWITCHED_AT}" in db.calls
    assert f"count_step_receipts_since:{SWITCHED_AT}" in db.calls
    # Measure 12 probed the largest enabled artifact.
    assert handler.probed == ["default-pipeline"]


def _plant(db, handler_kwargs: dict, plant: dict, tmp_path) -> None:
    source, value = plant["source"], plant["value"]
    if source == "parity":
        path = tmp_path / "parity-report.json"
        path.write_text(json.dumps(_parity_report(unexplained_field=value["field"])))
        handler_kwargs["parity_path"] = path
    elif source == "graph_latencies_ms":
        handler_kwargs["graph_latencies_ms"] = value
    elif source == "dashboard_tti_ms":
        for event in db.events:
            if event["kind"] == "window_coverage_rehearsal":
                event["detail"].pop("dashboard_tti_ms", None)
    elif source == "active_v1_runs":
        from tests.test_playbook_cutover import _run

        db.v1_runs = [_run(f"v1-{i}", "running", started_at=1.0) for i in range(value)]
    else:
        setattr(db, source, value)


@pytest.mark.parametrize(
    "case",
    json.loads((FIXTURES / "window-measures.json").read_text(encoding="utf-8")),
    ids=lambda case: f"measure-{case['measure']}",
)
@pytest.mark.asyncio
async def test_window_close_refuses_on_a_single_failing_measure(case, tmp_path):
    """T-12: a status that said ``pass`` is not trusted by the close.

    The status call runs first against healthy sources and passes; the source
    is then broken underneath it, and the close must refuse and name the
    measure — proof that it recomputed from source.
    """
    db = _WindowDB(_parity_sha())
    kwargs: dict = {}
    handler = _handler(db, **kwargs)
    before = await handler._cmd_playbook_cutover_window_status({})
    assert before["can_close"] is True, before["blocking_reasons"]

    _plant(db, kwargs, case["plant"], tmp_path)
    handler = _handler(db, **kwargs)

    result = await handler._cmd_playbook_cutover_window_close({"reason": "72h elapsed, looks fine"})

    assert result["success"] is False
    assert result["error"].startswith("window cannot close: 1 blocking condition(s)")
    assert any(case["expect_refusal_contains"] in r for r in result["blocking_reasons"]), (
        result["blocking_reasons"]
    )
    row = next(r for r in result["measures"] if r["measure"] == case["measure"])
    assert row["pass"] is False
    assert row["name"] == case["name"]
    assert all(r["pass"] for r in result["measures"] if r["measure"] != case["measure"])
    assert not any(e["kind"] == "rollback_window_closed" for e in db.events)
    assert result["window"]["elapsed_seconds"] == NOW - SWITCHED_AT


@pytest.mark.asyncio
async def test_window_close_records_the_measured_table_and_the_window():
    db = _WindowDB(_parity_sha())
    handler = _handler(db)

    result = await handler._cmd_playbook_cutover_window_close(
        {"reason": "72h elapsed; synthetic coverage from the rehearsal on 2026-09-03"}
    )

    assert result["success"] is True
    event = result["event"]
    assert event["kind"] == "rollback_window_closed"
    assert [row["measure"] for row in event["detail"]["measures"]] == list(range(1, 17))
    assert event["detail"]["window"]["since"] == SWITCHED_AT
    assert event["detail"]["window"]["observed_at"] == NOW
    assert event["detail"]["window"]["rehearsal_at"] == NOW - 3600.0
    assert db.events[-1] is event or db.events[-1]["event_id"] == event["event_id"]


@pytest.mark.asyncio
async def test_window_close_refuses_when_a_source_is_unreadable():
    db = _WindowDB(_parity_sha())
    db.step_counts = {"error": "receipts table unreadable"}
    handler = _handler(db)

    result = await handler._cmd_playbook_cutover_window_close({"reason": "closing the window"})

    assert result["success"] is False
    assert any("evidence unreadable" in r for r in result["blocking_reasons"])
    assert "receipts: receipts table unreadable" in result["evidence_errors"]


@pytest.mark.asyncio
async def test_window_status_reads_the_graph_probe_failure_as_unreadable():
    db = _WindowDB(_parity_sha())
    handler = _handler(db, graph_latencies_ms=RuntimeError("v2 api disabled"))

    status = await handler._cmd_playbook_cutover_window_status({})

    row = status["measures"][11]
    assert row["pass"] is False
    assert "evidence unreadable" in row["blocking"]
    assert "v2 api disabled" in row["blocking"]


@pytest.mark.asyncio
async def test_window_status_still_flags_a_runtime_flipped_by_hand():
    db = _WindowDB(_parity_sha())
    handler = _handler(db, _config(v2_engine=False))

    status = await handler._cmd_playbook_cutover_window_status({})

    assert "runtime flipped outside the cutover command" in status["blocking_reasons"]


@pytest.mark.asyncio
async def test_policy_free_switch_event_supplies_the_v1_baseline():
    """The switch event starts the window and owns its mechanical baseline."""
    from tests.test_playbook_cutover import _passing, _run

    db = _WindowDB(_parity_sha())
    db.events = []
    db.v1_runs = [
        _run("done-1", "completed", started_at=10.0, completed_at=12.0),
        _run("done-2", "completed", started_at=20.0, completed_at=25.0),
    ]
    config = _config(v2_engine=False, v1_admission="closed")
    handler = _handler(db, config)
    handler._cutover_check_report = _passing("cutover_report")
    handler._cutover_check_activations = _passing("activations")
    handler._cutover_check_pending_events = _passing("pending_events")

    async def _write(field, value):
        setattr(config.playbooks, field, value)

    handler._cutover_write_playbooks_field = _write

    result = await handler._cmd_playbook_cutover_switch(
        {"to": "v2", "reason": "cutting over after a clean drain"}
    )

    assert result["success"] is True, result
    assert [event["kind"] for event in db.events] == ["switched_to_v2"]
    assert result["event"]["detail"]["v1_baseline"] == {
        "sample_size": 2,
        "dispatch_p95": 5.0,
        "resume_p95": None,
    }

    status = await handler._cmd_playbook_cutover_window_status({})
    dispatch = next(row for row in status["measures"] if row["measure"] == 6)
    assert dispatch["observed"]["baseline_p95_ms"] == 5000.0


# ---------------------------------------------------------------------------
# T-11 — the coverage rehearsal
# ---------------------------------------------------------------------------


class _RehearsalEngine:
    def __init__(self, definitions):
        self.definitions = definitions
        self.dispatched: list[tuple[dict, frozenset]] = []
        self.services = SimpleNamespace(
            artifact_store=SimpleNamespace(load=lambda sha: self.definitions[sha])
        )

    async def dispatch_event(self, event, principal, mode=None, *, playbook_ids=None,
                             dispatch_id=None):
        self.dispatched.append((dict(event), frozenset(playbook_ids or ())))
        (playbook_id,) = tuple(playbook_ids)
        return SimpleNamespace(
            run_ids=(f"run-{playbook_id}-{len(self.dispatched)}",),
            rules_selected=("r",),
            pending=(),
            deduplicated=(),
        )


def _definition(playbook_id: str, event_types: list[str]):
    from src.playbooks.definition import PlaybookDefinition

    rules = [
        {"id": f"r{i}", "name": f"Rule {i}", "trigger": {"event_type": event_type},
         "entry_step": f"done-{i}", "source": {"path": "x.md", "start_line": 1, "end_line": 1}}
        for i, event_type in enumerate(event_types)
    ]
    steps = {
        f"done-{i}": {"type": "terminal", "rule": f"r{i}", "title": "Done", "outcome": "completed",
                      "source": {"path": "x.md", "start_line": 2, "end_line": 2}}
        for i in range(len(event_types))
    }
    return PlaybookDefinition.model_validate(
        {"schema_version": 2, "id": playbook_id, "version": 1, "scope": {"type": "system"},
         "source_hash": "sha256:" + "1" * 64, "compiled_at": "2026-09-01T00:00:00Z",
         "purpose": "routine", "rules": rules, "steps": steps}
    )


@pytest.mark.asyncio
async def test_rehearsal_dispatches_one_event_per_enabled_playbook():
    parity_sha = _parity_sha()
    db = _WindowDB(parity_sha)
    db.events = [e for e in db.events if e["kind"] != "window_coverage_rehearsal"]
    db.runs_by_playbook = {}
    engine = _RehearsalEngine(
        {
            parity_sha: _definition("default-pipeline", ["task.completed", "gate.resolved"]),
            "sha256:" + "a" * 64: _definition("memory-consolidation", ["timer.24h"]),
            "sha256:" + "b" * 64: _definition("coding-reflection", ["task.completed"]),
        }
    )
    handler = _handler(db)
    handler._cutover_rehearsal_engine = lambda: engine

    result = await handler._cmd_playbook_cutover_window_rehearsal(
        {"reason": "window coverage rehearsal 2026-09-03", "dashboard_tti_ms": 1100}
    )

    assert result["success"] is True, result
    assert sorted(result["playbooks"]) == [
        "coding-reflection", "default-pipeline", "memory-consolidation"
    ]
    # One synthetic event per enabled playbook, narrowed to that playbook.
    assert [sorted(ids) for _, ids in engine.dispatched] == [
        ["coding-reflection"], ["default-pipeline"], ["memory-consolidation"]
    ]
    for event, ids in engine.dispatched:
        assert event["_rehearsal"] is True
        assert event["event_id"].startswith("rehearsal-")
        assert isinstance(event["_received_at"], float)
    by_type = {next(iter(ids)): event["_event_type"] for event, ids in engine.dispatched}
    assert by_type["default-pipeline"] == "task.completed"  # the first rule's trigger
    assert by_type["memory-consolidation"] == "timer.24h"

    event = result["event"]
    assert event["kind"] == "window_coverage_rehearsal"
    assert sorted(event["detail"]["playbooks"]) == sorted(result["playbooks"])
    assert event["detail"]["dashboard_tti_ms"] == 1100.0
    assert event["detail"]["runs"]["default-pipeline"] == ["run-default-pipeline-2"]
    assert event["detail"]["uncovered"] == []
    assert db.events[-1]["kind"] == "window_coverage_rehearsal"


@pytest.mark.asyncio
async def test_rehearsal_names_playbooks_that_produced_no_run():
    parity_sha = _parity_sha()

    db = _WindowDB(parity_sha)
    engine = _RehearsalEngine(
        {
            parity_sha: _definition("default-pipeline", ["task.completed"]),
            "sha256:" + "a" * 64: _definition("memory-consolidation", ["timer.24h"]),
            "sha256:" + "b" * 64: _definition("coding-reflection", ["task.completed"]),
        }
    )

    async def _no_runs(event, principal, mode=None, *, playbook_ids=None, dispatch_id=None):
        engine.dispatched.append((dict(event), frozenset(playbook_ids or ())))
        return SimpleNamespace(run_ids=(), rules_selected=(), pending=(), deduplicated=())

    engine.dispatch_event = _no_runs
    handler = _handler(db)
    handler._cutover_rehearsal_engine = lambda: engine

    result = await handler._cmd_playbook_cutover_window_rehearsal(
        {"reason": "window coverage rehearsal 2026-09-03"}
    )

    assert result["success"] is True
    assert sorted(result["uncovered"]) == [
        "coding-reflection", "default-pipeline", "memory-consolidation"
    ]
    assert result["event"]["detail"]["uncovered"] == result["uncovered"]
    assert "dashboard_tti_ms" not in result["event"]["detail"]


@pytest.mark.asyncio
async def test_rehearsal_refuses_before_the_window_has_started():
    db = _WindowDB(_parity_sha())
    db.events = []
    handler = _handler(db)

    result = await handler._cmd_playbook_cutover_window_rehearsal(
        {"reason": "window coverage rehearsal 2026-09-03"}
    )

    assert result["success"] is False
    assert "switched_to_v2" in result["error"]


@pytest.mark.asyncio
async def test_rehearsal_refuses_without_a_real_reason():
    db = _WindowDB(_parity_sha())
    handler = _handler(db)
    before = len(db.events)
    result = await handler._cmd_playbook_cutover_window_rehearsal({"reason": "x"})
    assert result["success"] is False
    assert len(db.events) == before


@pytest.mark.asyncio
async def test_rehearsal_rejects_a_non_numeric_dashboard_tti():
    db = _WindowDB(_parity_sha())
    handler = _handler(db)
    result = await handler._cmd_playbook_cutover_window_rehearsal(
        {"reason": "window coverage rehearsal 2026-09-03", "dashboard_tti_ms": "fast"}
    )
    assert result["success"] is False
    assert "dashboard_tti_ms" in result["error"]


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_rehearsal_is_a_registered_operator_command():
    from src.commands.handler import PAUSED_PLAYBOOK_COMMANDS, CommandHandler
    from src.tools.definitions import _TOOL_CATEGORIES
    from tests.test_playbook_cutover import CUTOVER_COMMANDS

    assert "playbook_cutover_window_rehearsal" in CUTOVER_COMMANDS
    assert hasattr(CommandHandler, "_cmd_playbook_cutover_window_rehearsal")
    assert _TOOL_CATEGORIES.get("playbook_cutover_window_rehearsal") == "playbook"
    assert "playbook_cutover_window_rehearsal" not in PAUSED_PLAYBOOK_COMMANDS


@pytest.mark.asyncio
async def test_window_response_dtos_accept_the_real_output(tmp_path):
    from src.api.models import get_all_response_models

    models = get_all_response_models()
    db = _WindowDB(_parity_sha())
    handler = _handler(db)
    status = await handler._cmd_playbook_cutover_window_status({})
    models["playbook_cutover_window_status"].model_validate(status)

    db.pending = {"count": 9, "oldest_received_at": "recent"}
    refused = await handler._cmd_playbook_cutover_window_close({"reason": "closing the window"})
    models["playbook_cutover_window_close"].model_validate(refused)

    db.pending = {"count": 0, "oldest_received_at": None}
    closed = await handler._cmd_playbook_cutover_window_close({"reason": "closing the window"})
    assert closed["success"] is True
    models["playbook_cutover_window_close"].model_validate(closed)

    engine = _RehearsalEngine({_parity_sha(): _definition("default-pipeline", ["task.completed"]),
                               "sha256:" + "a" * 64: _definition("memory-consolidation", ["t"]),
                               "sha256:" + "b" * 64: _definition("coding-reflection", ["t"])})
    handler._cutover_rehearsal_engine = lambda: engine
    rehearsed = await handler._cmd_playbook_cutover_window_rehearsal(
        {"reason": "window coverage rehearsal 2026-09-03"}
    )
    models["playbook_cutover_window_rehearsal"].model_validate(rehearsed)
    models["playbook_cutover_window_rehearsal"].model_validate(
        await handler._cmd_playbook_cutover_window_rehearsal({"reason": "x"})
    )
