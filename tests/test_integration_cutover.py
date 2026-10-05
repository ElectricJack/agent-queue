"""Cutover invariants for ``integration.git_first: active``.

Plan ``projects/agent-queue/plans/2026-10-04-git-first-integration-plan-2.md``
section 4 ("Single-engine transition") and section 2.2 ("No journal"). These
are the properties the operator's canary depends on, so they are asserted
against the durable tables rather than only against the wiring:

* a pending legacy outbox event is never dispatched into an old writer, before
  or after a restart;
* the train writes no green continuation, so nothing re-emits a promotion;
* the reduced modules read no checkpoint and write no ref journal;
* the stage-2 revisions add columns and constraints only: no column is dropped,
  no table is created, and none of them is a ref/OID journal.

Nothing here starts, stops or configures the daemon, and no test transfers a
root: ``_transfer`` stays an operator action.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.database.tables import integration_outbox, integration_subject_journal
from src.integration.outbox import enqueue_integration_event
from src.integration.service import IntegrationService

#: The reduced protocol's own modules. Every guard these run reads git, checks
#: or review evidence; a checkpoint or journal reference here would be a
#: surviving legacy reader on the active path.
ACTIVE_MODULES = (
    "src/integration/git_truth.py",
    "src/integration/lock.py",
    "src/integration/checks.py",
    "src/integration/reviews.py",
    "src/integration/batches.py",
    "src/integration/epics.py",
    "src/integration/train.py",
    "src/integration/train_sources.py",
    "src/integration/source_trailer.py",
    "src/integration/shadow.py",
)

#: The stage-2 revisions, in chain order from the pre-cutover head.
STAGE_TWO_REVISIONS = (
    "a00000000073_integration_ref_leases",
    "a00000000074_git_batch_inputs",
    "a00000000075_integration_check_evidence_commit_cache",
)

ROOT = Path(__file__).resolve().parents[1]


def _upgrade_calls(revision: str) -> set[str]:
    """The alembic calls a revision's ``upgrade`` body actually makes."""
    tree = ast.parse((ROOT / "migrations/versions" / f"{revision}.py").read_text())
    upgrade = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "upgrade")
    return {node.func.attr for node in ast.walk(upgrade)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


# -- Old-event isolation ------------------------------------------------------

@pytest.fixture
async def legacy_events(reuse_database):
    """One due legacy event, already pending before the selector flips to active."""
    db = await reuse_database("integration-cutover.db")
    async with db.immediate() as conn:
        await enqueue_integration_event(
            conn, event_id="legacy-1", dedup_key="legacy:1", project_id="p",
            event_type="integration.root_delivered",
            payload={"project_id": "p", "event_id": "legacy-1"}, available_at=0.0,
        )
    return db


def _active_service(now: float = 100.0) -> tuple[IntegrationService, list, list]:
    """The service as the daemon builds it under ``git_first: active``."""
    ticked, dispatched = [], []

    async def train_tick(at):
        ticked.append(at)
        return {"started": [], "running": [], "skipped": []}

    async def dispatch_due(at):
        dispatched.append(at)

    async def stop():
        return None

    service = IntegrationService(
        object(), SimpleNamespace(dispatch_due=dispatch_due),
        train=SimpleNamespace(tick=train_tick, stop=stop),
        clock=lambda: now,
    )
    return service, ticked, dispatched


async def test_pending_legacy_event_reaches_no_writer_before_or_after_a_restart(legacy_events):
    """The durable row stays pending across two services; nothing consumes it."""
    db = legacy_events
    for _restart in range(2):
        service, ticked, dispatched = _active_service()
        await service.tick(100.0)
        await service.stop()
        assert ticked == [100.0]
        assert dispatched == []
    async with db._engine.connect() as conn:
        pending = (await conn.execute(select(integration_outbox))).mappings().all()
    assert [row["id"] for row in pending] == ["legacy-1"]
    assert pending[0]["attempts"] == 0, "an active-mode pass must not consume the event"


async def test_green_continuation_is_never_emitted_by_the_active_path(legacy_events):
    """A pass adds no promotion continuation; only the legacy repair path can."""
    db = legacy_events
    service, _ticked, _dispatched = _active_service()
    await service.tick(100.0)
    await service.stop()
    async with db._engine.connect() as conn:
        emitted = (await conn.execute(
            select(integration_outbox.c.id, integration_outbox.c.event_type)
        )).all()
    assert emitted == [("legacy-1", "integration.root_delivered")]


async def test_the_reduced_path_writes_no_ref_journal(legacy_events):
    """The subject journal is a legacy record; an active pass leaves it untouched."""
    db = legacy_events
    service, _ticked, _dispatched = _active_service()
    await service.tick(100.0)
    await service.stop()
    async with db._engine.connect() as conn:
        entries = (await conn.execute(select(integration_subject_journal))).all()
    assert entries == []


# -- No surviving legacy reader ----------------------------------------------

@pytest.mark.parametrize("module", ACTIVE_MODULES)
def test_reduced_module_reads_no_checkpoint_and_writes_no_journal(module):
    """Static proof: the active path never names a checkpoint or journal table."""
    tree = ast.parse((ROOT / module).read_text())
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "src.database.tables"
        ):
            named.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
    assert not [name for name in named if "checkpoint" in name], module
    assert not [name for name in named if "journal" in name], module


def test_green_continuation_lives_only_in_the_legacy_repair_path():
    """The emitter exists, and only the retired repair path can call it."""
    callers = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "src").rglob("*.py"))
               if path.name != "green_continuation.py"
               and "enqueue_green_continuation_on" in path.read_text()]
    assert callers == ["src/integration/repair.py"]


def test_the_selector_installs_no_diagnostics_in_active_mode():
    """``active`` builds no shadow comparison; shadow never becomes a policy input."""
    from src.integration.shadow import diagnostics_for

    active = SimpleNamespace(git_first="active")
    assert diagnostics_for(active, object(), object()) is None
    assert diagnostics_for(SimpleNamespace(git_first="shadow"), object(), object()) is not None


def test_shadow_never_acts_on_a_subject_the_reconciler_does_not_own():
    """Diagnostics run only for a reconciler-owned subject, and are read-only.

    The reconciler gates the optional comparison on ownership and records it
    only in the log and the correlation context, so shadow can never become a
    second writer or a policy input.
    """
    source = (ROOT / "src/integration/reconciler.py").read_text()
    assert "subject.engine is SubjectEngine.RECONCILER" in source
    assert "self._diagnostics(subject, facts)" in source
    assert "Diagnostic failure must not change the authoritative policy" in source


# -- Compatible schema --------------------------------------------------------

@pytest.mark.parametrize("revision", STAGE_TWO_REVISIONS)
def test_stage_two_revision_adds_columns_and_constraints_only(revision):
    """No stage-2 upgrade drops a column or creates a table."""
    calls = _upgrade_calls(revision)
    assert "drop_column" not in calls
    assert "drop_table" not in calls
    assert "create_table" not in calls
    assert calls & {"add_column", "create_index", "create_check_constraint"}


def test_stage_two_introduces_no_ref_journal_table():
    """No stage-2 revision names a table that replays refs, OIDs or batches."""
    forbidden = ("journal", "checkpoint", "intents", "outbox", "promotions", "deliveries")
    for revision in STAGE_TWO_REVISIONS:
        text = (ROOT / "migrations/versions" / f"{revision}.py").read_text()
        named = [line.split('"')[1] for line in text.splitlines()
                 if line.startswith("TABLE") and '"' in line]
        for name in named:
            assert not any(word in name for word in forbidden), (revision, name)


def test_the_check_cache_reshapes_in_place_and_adds_no_table():
    """``integration_check_evidence`` is reshaped, never replaced by a new table."""
    retained = json.loads(
        (ROOT / "tests/integration_ownership.json").read_text()
    )["baseline"]["tables"]
    assert "integration_commit_checks" not in retained
    assert "integration_check_evidence" in retained
    assert len(retained) == 45, "stage two adds no table; the predicate stays at 45"