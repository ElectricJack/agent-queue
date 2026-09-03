"""Playbook V2 Package 7 commit 1 — the V1 drain surface.

Child plan §5.1 (``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-
cleanup.md``): T-1 module boundary, T-2 ownership classification, T-3 a cancel
that actually cancels, T-4 the drain works on a paused subsystem, T-5 admission
close and the V1 baseline, T-6 operator-only.
"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.commands.handler import PLAYBOOKS_PAUSED_ERROR
from src.commands.playbook_cutover_commands import REASON_TOO_SHORT_ERROR
from src.models import PlaybookRun
from src.playbooks.cutover import (
    DrainStatus,
    drain_status,
    playbook_runtime,
    v1_admission_closed,
    v1_latency_baseline,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The seven §3.3 commands.  Every one of them must answer on a paused fleet.
CUTOVER_COMMANDS = (
    "playbook_v1_drain_status",
    "playbook_v1_admission_close",
    "playbook_v1_admission_open",
    "playbook_v1_run_cancel",
    "playbook_cutover_switch",
    "playbook_cutover_window_status",
    "playbook_cutover_window_close",
)


def _module_imports(relative: str) -> set[str]:
    """Every module named by a top-level ``import`` in one source file."""
    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _module_scope_imports(relative: str) -> set[str]:
    """Only the imports at module scope — a lazy import inside a function is
    not a module-level dependency and does not create an import cycle."""
    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _config(*, enabled=True, v2_engine=False, v1_admission="open"):
    from src.config import PlaybooksConfig

    return SimpleNamespace(
        playbooks=PlaybooksConfig(
            enabled=enabled, v2_engine=v2_engine, v1_admission=v1_admission
        )
    )


def _run(run_id, status, *, started_at, playbook_id="pb", **kwargs):
    return PlaybookRun(
        run_id=run_id,
        playbook_id=playbook_id,
        playbook_version=1,
        trigger_event=kwargs.pop("trigger_event", json.dumps({"type": "t"})),
        status=status,
        current_node=kwargs.pop("current_node", "n1"),
        conversation_history="[]",
        node_trace="[]",
        tokens_used=0,
        started_at=started_at,
        completed_at=kwargs.pop("completed_at", None),
        error=None,
        pinned_graph=None,
        paused_at=kwargs.pop("paused_at", None),
        waiting_for_event=kwargs.pop("waiting_for_event", None),
        event_id=kwargs.pop("event_id", None),
    )


class _FakeDB:
    """The three reads and one write ``drain_status`` and the commands make."""

    def __init__(self, runs=(), events=()):
        self.runs = list(runs)
        self.events = list(events)

    async def list_playbook_runs(self, playbook_id=None, status=None, limit=50):
        return [r for r in self.runs if status is None or r.status == status][:limit]

    async def get_playbook_run(self, run_id):
        return next((r for r in self.runs if r.run_id == run_id), None)

    async def update_playbook_run(self, run_id, **kwargs):
        for index, run in enumerate(self.runs):
            if run.run_id == run_id:
                self.runs[index] = PlaybookRun(
                    **{**run.__dict__, **kwargs}
                )

    async def latest_playbook_cutover_event(self, kind):
        matches = [e for e in self.events if e["kind"] == kind]
        return max(matches, key=lambda e: e["at"]) if matches else None

    async def append_playbook_cutover_event(self, *, kind, actor, reason, detail=None, at=None):
        event = {
            "event_id": f"e{len(self.events)}",
            "kind": kind,
            "at": at if at is not None else time.time(),
            "actor": actor,
            "reason": reason,
            "detail": dict(detail or {}),
        }
        self.events.append(event)
        return event


# ---------------------------------------------------------------------------
# T-1 — module boundary
# ---------------------------------------------------------------------------


def test_cutover_does_not_import_engine_or_create_cycle():
    """§2.1: cutover -> migration, one way, and never the engine at import time.

    A cycle here would make the readiness report depend on operational state
    and vice versa, and neither could then be used as evidence for the other's
    gate.  The engine exclusion is what lets the drain run on a paused fleet.
    """
    cutover = _module_imports("src/playbooks/cutover.py")
    assert "src.playbooks.migration" in cutover

    migration = _module_imports("src/playbooks/migration.py")
    assert "src.playbooks.cutover" not in migration

    assert "src.playbooks.engine" not in _module_scope_imports("src/playbooks/cutover.py")


# ---------------------------------------------------------------------------
# T-2 — ownership classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_status_marks_unowned_running_rows_orphaned():
    db = _FakeDB(
        [
            _run("live-1", "running", started_at=100.0),
            _run("dead-1", "running", started_at=200.0),
            _run("dead-2", "paused", started_at=300.0, paused_at=350.0),
        ]
    )
    manager = SimpleNamespace(running_runs=lambda: {"live-1": "pb"})

    status = await drain_status(db=db, manager=manager, config=_config(), clock=lambda: 1000.0)

    assert [r.ownership for r in status.active] == ["live", "orphaned", "orphaned"]
    assert status.live_count == 1
    assert status.orphaned_count == 2
    assert status.drained is False
    assert status.oldest_age_seconds == 900.0
    # Never empty, and "wait" is offered only where a coroutine can still finish.
    assert status.active[0].options == ("wait", "cancel")
    assert status.active[1].options == ("cancel",)
    assert status.active[2].options == ("resolve", "cancel")


@pytest.mark.asyncio
async def test_drain_status_without_manager_marks_everything_orphaned():
    """§3.2: ``manager=None`` means orphaned, not unknown.

    A caller that cannot prove a coroutine exists must not be able to make
    ``drained`` go true just because nobody was looking.
    """
    db = _FakeDB(
        [
            _run("a", "running", started_at=1.0),
            _run("b", "running", started_at=2.0),
            _run("c", "paused", started_at=3.0),
        ]
    )
    status = await drain_status(db=db, manager=None, config=_config(), clock=lambda: 10.0)
    assert {r.ownership for r in status.active} == {"orphaned"}
    assert status.live_count == 0
    assert status.orphaned_count == 3


@pytest.mark.asyncio
async def test_drain_status_is_drained_only_when_admission_is_closed_too():
    """Zero active runs alone is a snapshot; the gate is zero *and* closed."""
    db = _FakeDB([])
    open_fleet = await drain_status(db=db, manager=None, config=_config(), clock=lambda: 1.0)
    assert open_fleet.drained is False
    assert open_fleet.admission == "open"

    closed = await drain_status(
        db=db, manager=None, config=_config(v1_admission="closed"), clock=lambda: 1.0
    )
    assert closed.drained is True
    assert closed.admission == "closed"


@pytest.mark.asyncio
async def test_drain_status_reports_the_project_a_run_was_triggered_for():
    db = _FakeDB(
        [
            _run(
                "r1",
                "running",
                started_at=1.0,
                trigger_event=json.dumps({"type": "t", "project_id": "proj-a"}),
            ),
            _run("r2", "running", started_at=2.0, trigger_event="not json at all"),
        ]
    )
    status = await drain_status(db=db, manager=None, config=_config(), clock=lambda: 5.0)
    assert [r.project_id for r in status.active] == ["proj-a", None]


def test_drain_status_to_dict_has_a_stable_shape():
    status = DrainStatus(
        generated_at=1.0,
        admission="closed",
        closed_at=None,
        closed_by=None,
        active=(),
        live_count=0,
        orphaned_count=0,
        oldest_age_seconds=None,
        drained=True,
    )
    assert list(status.to_dict()) == [
        "generated_at",
        "admission",
        "closed_at",
        "closed_by",
        "active",
        "live_count",
        "orphaned_count",
        "oldest_age_seconds",
        "drained",
    ]


# ---------------------------------------------------------------------------
# Selectors (§3.4)
# ---------------------------------------------------------------------------


def test_selectors_are_independent():
    """Draining happens *while* the fleet is on V1, and a rollback flips the
    runtime back without reopening admission."""
    assert playbook_runtime(_config()) == "v1"
    assert playbook_runtime(_config(v2_engine=True, v1_admission="closed")) == "v2"
    # A paused subsystem is never "on v2", whatever the flag says.
    assert playbook_runtime(_config(enabled=False, v2_engine=True)) == "v1"

    assert v1_admission_closed(_config()) is False
    assert v1_admission_closed(_config(v1_admission="closed")) is True
    # The supported rollback state: back on v1, admission still closed.
    rollback = _config(v2_engine=False, v1_admission="closed")
    assert playbook_runtime(rollback) == "v1"
    assert v1_admission_closed(rollback) is True


def test_v1_latency_baseline_is_computed_from_the_drained_rows():
    runs = [
        _run("a", "completed", started_at=0.0, completed_at=10.0),
        _run("b", "completed", started_at=0.0, completed_at=20.0),
        _run("c", "completed", started_at=0.0, completed_at=30.0, paused_at=25.0),
        _run("d", "running", started_at=0.0),
    ]
    baseline = v1_latency_baseline(runs)
    assert baseline["sample_size"] == 3
    assert baseline["dispatch_p95"] == 30.0
    assert baseline["resume_p95"] == 5.0
    assert v1_latency_baseline([]) == {
        "sample_size": 0,
        "dispatch_p95": None,
        "resume_p95": None,
    }


# ---------------------------------------------------------------------------
# T-3 — a cancel that actually cancels
# ---------------------------------------------------------------------------


def _cutover_handler(db, config):
    """A bare mixin instance — the drain surface needs no orchestrator."""
    from src.commands.playbook_cutover_commands import PlaybookCutoverCommandsMixin

    class _Handler(PlaybookCutoverCommandsMixin):
        def __init__(self):
            self.db = db
            self.config = config
            self.orchestrator = SimpleNamespace(playbook_manager=None, bus=None)

    return _Handler()


@pytest.mark.asyncio
async def test_v1_run_cancel_survives_the_runners_final_write():
    """§1.2 is the defect this command exists to fix.

    ``cancel_playbook_run`` writes ``cancelled`` but cannot stop the coroutine,
    so the runner's next persistence write puts the row back to ``running`` —
    a drain would report zero and then watch the count climb, silently.  The
    fix is ordering: join the cancelled task *before* the terminal write.
    """
    db = _FakeDB([_run("r1", "running", started_at=1.0)])
    started = asyncio.Event()

    async def _runner():
        try:
            started.set()
            await asyncio.sleep(3600)
        finally:
            # Exactly what today's V1 runner does on its way out.
            await db.update_playbook_run("r1", status="running")

    task = asyncio.create_task(_runner())
    await started.wait()

    handler = _cutover_handler(db, _config())
    handler.orchestrator = SimpleNamespace(
        playbook_manager=SimpleNamespace(_running={"r1": task}, running_runs=lambda: {"r1": "pb"}),
        bus=None,
    )

    result = await handler._cmd_playbook_v1_run_cancel(
        {"run_id": "r1", "reason": "draining for the v2 cutover"}
    )

    assert result["success"] is True
    assert result["ownership"] == "live"
    assert task.done()
    assert (await db.get_playbook_run("r1")).status == "cancelled"


@pytest.mark.asyncio
async def test_v1_run_cancel_refuses_when_the_task_will_not_stop(monkeypatch):
    """A half-cancelled run must not be reported drained."""
    monkeypatch.setattr(
        "src.commands.playbook_cutover_commands.CANCEL_JOIN_TIMEOUT", 0.05, raising=False
    )
    db = _FakeDB([_run("r1", "running", started_at=1.0)])
    started = asyncio.Event()

    swallowed = 0

    async def _stubborn():
        nonlocal swallowed
        started.set()
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                swallowed += 1
                if swallowed > 1:
                    # Only the drain's cancellation is swallowed; the test's
                    # own cleanup must still be able to reap the task.
                    raise

    task = asyncio.create_task(_stubborn())
    await started.wait()

    handler = _cutover_handler(db, _config())
    handler.orchestrator = SimpleNamespace(
        playbook_manager=SimpleNamespace(_running={"r1": task}), bus=None
    )
    result = await handler._cmd_playbook_v1_run_cancel(
        {"run_id": "r1", "reason": "draining for the v2 cutover"}
    )

    assert result["success"] is False
    assert "did not stop" in result["error"]
    assert (await db.get_playbook_run("r1")).status == "running"

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_v1_run_cancel_clears_an_orphaned_row():
    """The drain's whole point: a row no coroutine owns still needs a
    terminal write, and waiting will never produce one."""
    db = _FakeDB([_run("orphan", "running", started_at=1.0)])
    handler = _cutover_handler(db, _config())
    result = await handler._cmd_playbook_v1_run_cancel(
        {"run_id": "orphan", "reason": "orphaned by a daemon restart"}
    )
    assert result["success"] is True
    assert result["ownership"] == "orphaned"
    assert (await db.get_playbook_run("orphan")).status == "cancelled"


@pytest.mark.asyncio
async def test_v1_run_cancel_rejects_bad_input():
    db = _FakeDB([_run("done", "completed", started_at=1.0, completed_at=2.0)])
    handler = _cutover_handler(db, _config())

    assert (await handler._cmd_playbook_v1_run_cancel({"reason": "a" * 20}))["error"] == (
        "run_id is required"
    )
    short = await handler._cmd_playbook_v1_run_cancel({"run_id": "done", "reason": "no"})
    assert short["error"] == REASON_TOO_SHORT_ERROR
    missing = await handler._cmd_playbook_v1_run_cancel(
        {"run_id": "nope", "reason": "draining for the v2 cutover"}
    )
    assert "no playbook run" in missing["error"]
    terminal = await handler._cmd_playbook_v1_run_cancel(
        {"run_id": "done", "reason": "draining for the v2 cutover"}
    )
    assert "already terminal" in terminal["error"]


# ---------------------------------------------------------------------------
# T-4 — the drain works on a paused subsystem
# ---------------------------------------------------------------------------


def test_drain_commands_are_not_paused_with_the_subsystem():
    """§3.3: the one place this package widens a surface, deliberately.

    ``playbooks.enabled`` defaults to False, and a fleet that paused the
    subsystem with runs still ``running`` must still be able to see and clear
    them.  Draining is exactly the operation you need when the subsystem is off.
    """
    from src.commands.handler import PAUSED_PLAYBOOK_COMMANDS

    assert PAUSED_PLAYBOOK_COMMANDS.isdisjoint(CUTOVER_COMMANDS)
    assert "list_playbooks" in PAUSED_PLAYBOOK_COMMANDS


@pytest.mark.asyncio
async def test_drain_commands_answer_while_playbooks_disabled(command_handler_factory):
    handler = await command_handler_factory()
    handler.config.playbooks.enabled = False

    for name in CUTOVER_COMMANDS:
        result = await handler.execute(name, {"reason": "x" * 20, "run_id": "r", "to": "v2"})
        assert result != PLAYBOOKS_PAUSED_ERROR, name
        assert not (isinstance(result, dict) and result.get("error") == PLAYBOOKS_PAUSED_ERROR), name

    paused = await handler.execute("list_playbooks", {})
    assert paused == PLAYBOOKS_PAUSED_ERROR or (
        isinstance(paused, dict) and paused.get("error") == PLAYBOOKS_PAUSED_ERROR
    )


@pytest.mark.asyncio
async def test_every_cutover_command_is_reachable_and_categorised():
    from src.commands.handler import CommandHandler
    from src.tools.definitions import _TOOL_CATEGORIES

    for name in CUTOVER_COMMANDS:
        assert hasattr(CommandHandler, f"_cmd_{name}"), name
        assert _TOOL_CATEGORIES.get(name) == "playbook", name


# ---------------------------------------------------------------------------
# T-5 — admission close, and the baseline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_close_refuses_without_a_real_reason():
    handler = _cutover_handler(_FakeDB([]), _config())
    for reason in ("", "drain", "short"):
        result = await handler._cmd_playbook_v1_admission_close({"reason": reason})
        assert result == {"success": False, "error": REASON_TOO_SHORT_ERROR}


@pytest.mark.asyncio
async def test_admission_close_records_an_audit_row_and_flips_the_selector(monkeypatch):
    db = _FakeDB([_run("r1", "running", started_at=1.0)])
    config = _config()
    handler = _cutover_handler(db, config)

    async def _write(field, value):
        setattr(config.playbooks, field, value)
        return None

    monkeypatch.setattr(handler, "_cutover_write_playbooks_field", _write)

    result = await handler._cmd_playbook_v1_admission_close(
        {"reason": "closing v1 ahead of the cutover"}
    )
    assert result["success"] is True
    assert v1_admission_closed(config) is True
    assert result["admission"] == "closed"
    assert db.events[-1]["kind"] == "v1_admission_closed"
    assert db.events[-1]["reason"] == "closing v1 ahead of the cutover"
    assert db.events[-1]["detail"]["orphaned_count"] == 1

    again = await handler._cmd_playbook_v1_admission_close({"reason": "closing it twice over"})
    assert again == {"success": False, "error": "v1 admission is already closed"}


@pytest.mark.asyncio
async def test_admission_cannot_be_reopened_while_the_fleet_is_on_v2():
    """§4.3: ``runtime=v2`` with admission open would let a rollback silently
    start new V1 runs against artifacts nobody reviewed."""
    config = _config(v2_engine=True, v1_admission="closed")
    handler = _cutover_handler(_FakeDB([]), config)
    result = await handler._cmd_playbook_v1_admission_open({"reason": "rolling back for now"})
    assert result["success"] is False
    assert "cannot be re-opened while the fleet is on v2" in result["error"]


@pytest.mark.asyncio
async def test_admission_close_blocks_new_v1_dispatch(monkeypatch, caplog):
    """The guard sits above every V1 import in ``_on_playbook_trigger``."""
    from src.orchestrator.core import Orchestrator

    orch = object.__new__(Orchestrator)
    orch.config = _config(v1_admission="closed")
    orch.db = MagicMock()
    orch._command_handler = None

    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("V1 runner was constructed with admission closed")

    monkeypatch.setattr("src.playbooks.runner.PlaybookRunner", _boom, raising=False)

    with caplog.at_level("INFO"):
        await orch._on_playbook_trigger(SimpleNamespace(id="pb", to_dict=lambda: {}), {"type": "t"})

    assert "v1 admission closed" in caplog.text
    orch.db.create_playbook_run.assert_not_called()


@pytest.mark.asyncio
async def test_run_playbook_refuses_new_v1_runs_while_admission_is_closed(
    command_handler_factory,
):
    """A drain an operator can undo by hand-running a playbook is not a drain."""
    from src.commands.playbook_commands import V1_ADMISSION_CLOSED_ERROR

    handler = await command_handler_factory()
    handler.config.playbooks.enabled = True
    handler.config.playbooks.v1_admission = "closed"

    playbook = SimpleNamespace(id="pb", enabled=True, to_dict=lambda: {"kind": "pipeline"})
    handler.orchestrator.playbook_manager = SimpleNamespace(
        get_playbook=lambda _id: playbook,
        get_scope_identifier=lambda _id: None,
    )

    result = await handler.execute("run_playbook", {"playbook_id": "pb", "event": {"type": "t"}})
    assert result == {"error": V1_ADMISSION_CLOSED_ERROR}

    # ...and the same call succeeds past the guard once admission re-opens,
    # so the test pins the guard rather than an unrelated failure.
    handler.config.playbooks.v1_admission = "open"
    reopened = await handler.execute(
        "run_playbook", {"playbook_id": "pb", "event": {"type": "t"}}
    )
    assert reopened != {"error": V1_ADMISSION_CLOSED_ERROR}


# ---------------------------------------------------------------------------
# The switch and the window gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cutover_switch_refuses_v2_before_the_drain_completes():
    db = _FakeDB([_run("r1", "running", started_at=1.0)])
    handler = _cutover_handler(db, _config(v1_admission="closed"))
    result = await handler._cmd_playbook_cutover_switch(
        {"to": "v2", "reason": "cutting over to the v2 engine"}
    )
    assert result["success"] is False
    assert "before the drain completes" in result["error"]
    assert db.events == []


@pytest.mark.asyncio
async def test_cutover_switch_to_v1_is_refused_once_the_window_is_closed():
    db = _FakeDB(
        [],
        [{"event_id": "e", "kind": "rollback_window_closed", "at": 5.0, "actor": "op",
          "reason": "r", "detail": {}}],
    )
    handler = _cutover_handler(db, _config(v2_engine=True, v1_admission="closed"))
    result = await handler._cmd_playbook_cutover_switch(
        {"to": "v1", "reason": "trying to roll back late"}
    )
    assert result["success"] is False
    assert "rollback window closed" in result["error"]


@pytest.mark.asyncio
async def test_window_close_refuses_and_names_every_blocking_measure():
    """§3.9: the gate recomputes; it never trusts a stored verdict.  A measure
    whose source is not wired is reported as not passing, never as fine."""
    db = _FakeDB([_run("r1", "running", started_at=1.0)])
    handler = _cutover_handler(db, _config(v2_engine=True, v1_admission="closed"))

    status = await handler._cmd_playbook_cutover_window_status({})
    assert status["can_close"] is False
    assert any("measure 16" in reason for reason in status["blocking_reasons"])
    assert all(not row["pass"] for row in status["measures"] if row["measure"] != 16)
    assert [row["measure"] for row in status["measures"]] == list(range(1, 17))

    result = await handler._cmd_playbook_cutover_window_close({"reason": "closing the window"})
    assert result["success"] is False
    assert result["blocking_reasons"]
    assert db.events == []


@pytest.mark.asyncio
async def test_window_status_detects_a_runtime_flipped_outside_the_command():
    """§3.9: an operator must be able to roll back at 3am without a gate row,
    so a hand edit is detected rather than prevented."""
    db = _FakeDB(
        [],
        [{"event_id": "e", "kind": "switched_to_v2", "at": 1.0, "actor": "op",
          "reason": "r", "detail": {}}],
    )
    handler = _cutover_handler(db, _config(v2_engine=False, v1_admission="closed"))
    status = await handler._cmd_playbook_cutover_window_status({})
    assert "runtime flipped outside the cutover command" in status["blocking_reasons"]


# ---------------------------------------------------------------------------
# T-6 — operator-only
# ---------------------------------------------------------------------------


def test_cutover_commands_are_in_no_shipped_profile():
    """The ratchet against a later profile edit handing an agent the switch."""
    offenders: list[str] = []
    for profile in sorted(
        (REPO_ROOT / "src" / "profiles" / "defaults").glob("*/profile.md")
    ):
        text = profile.read_text(encoding="utf-8")
        for name in CUTOVER_COMMANDS:
            if name in text:
                offenders.append(f"{profile.parent.name}: {name}")
    assert offenders == []


# ---------------------------------------------------------------------------
# The typed API surface must accept what the commands actually return
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_response_dto_accepts_its_commands_real_output():
    """The DTOs are ``extra="forbid"``, so a key the command emits and the
    model does not declare is a 500 on the generated route rather than a
    warning.  This drives each command for real and validates the result."""
    from src.api.models import get_all_response_models

    models = get_all_response_models()

    async def _validate(name, payload):
        models[name].model_validate(payload)

    # A drained fleet, an active fleet, and every refusal path.
    active_db = _FakeDB([_run("live-1", "running", started_at=1.0)])
    active = _cutover_handler(active_db, _config(v1_admission="closed"))
    active.orchestrator = SimpleNamespace(
        playbook_manager=SimpleNamespace(running_runs=lambda: {"live-1": "pb"}, _running={}),
        bus=None,
    )
    await _validate("playbook_v1_drain_status", await active._cmd_playbook_v1_drain_status({}))
    await _validate(
        "playbook_cutover_switch",
        await active._cmd_playbook_cutover_switch({"to": "v2", "reason": "cutting over now"}),
    )
    await _validate(
        "playbook_v1_run_cancel",
        await active._cmd_playbook_v1_run_cancel(
            {"run_id": "live-1", "reason": "draining for the cutover"}
        ),
    )
    await _validate(
        "playbook_v1_run_cancel",
        await active._cmd_playbook_v1_run_cancel({"reason": "draining for the cutover"}),
    )
    await _validate(
        "playbook_v1_admission_close",
        await active._cmd_playbook_v1_admission_close({"reason": "already closed here"}),
    )

    window = _cutover_handler(_FakeDB([]), _config(v2_engine=True, v1_admission="closed"))
    await _validate(
        "playbook_cutover_window_status",
        await window._cmd_playbook_cutover_window_status({}),
    )
    await _validate(
        "playbook_cutover_window_close",
        await window._cmd_playbook_cutover_window_close({"reason": "closing the window"}),
    )
    await _validate(
        "playbook_v1_admission_open",
        await window._cmd_playbook_v1_admission_open({"reason": "rolling back for now"}),
    )

    # The two success shapes, which need a config writer.  These are the
    # payloads a real operator sees, so leaving them unvalidated would leave
    # the likeliest 500 uncovered.
    open_config = _config()
    writable = _cutover_handler(_FakeDB([]), open_config)

    async def _write(field, value):
        setattr(open_config.playbooks, field, value)
        return None

    writable._cutover_write_playbooks_field = _write
    await _validate(
        "playbook_v1_admission_close",
        await writable._cmd_playbook_v1_admission_close({"reason": "closing ahead of cutover"}),
    )
    switched = await writable._cmd_playbook_cutover_switch(
        {"to": "v2", "reason": "cutting over after a clean drain"}
    )
    assert switched["success"] is True
    await _validate("playbook_cutover_switch", switched)
    await _validate(
        "playbook_v1_admission_open",
        await writable._cmd_playbook_v1_admission_open({"reason": "reopening after rollback"}),
    )
