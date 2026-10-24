"""Recorded old/new provider-failover scenarios through the V2 command executor.

The outcome matrix substitutes domain responses, not command adapters or the
engine. The database case also keeps the real handler and notification service
so a source capability declaration cannot silently refuse an existing command.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.contracts import CONTRACTS, builtin
from src.commands.principal import TRUSTED_LOCAL
from src.playbooks.definition import load_definition_json, source_digest
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.providers.availability import STARTUP_DIALOG
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    StubActivations,
    artifact_ref_for,
)

FIXTURES = Path(__file__).parent / "fixtures"
PORT = FIXTURES / "provider_failover" / "playbook-port"
CURRENT = FIXTURES / "playbooks" / "v2" / "provider-failover" / "artifact.json"
BASELINE = PORT / "baseline-artifact.json"
SCENARIOS = json.loads((PORT / "scenarios.json").read_text())


def engine_for(path, handler):
    artifact = load_definition_json(path.read_text())
    store = InMemoryArtifactStore()
    store.put(artifact)
    runs = RecordingRunRepository()
    engine = PlaybookEngine(
        services=EngineServices(
            contracts=CONTRACTS, handler=handler, artifact_store=store,
            private_values=AsyncMock(),
            clock=lambda: 1_800_000_000.0,
        ),
        runs=runs, waits=None, activations=StubActivations([artifact_ref_for(artifact)]),
    )
    return engine, runs


def test_baseline_is_the_authorized_installed_source_and_recorded_artifact():
    source = (PORT / "baseline-source.md").read_bytes()
    assert hashlib.sha256(source).hexdigest() == (
        "42667d84239f92d17e3713b114c72a36706423ffb02b61c6e385fef9e4a5f0e0"
    )
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == (
        "e4c946b8eddf8fcbac5ed552887091e1eb822dd851a1647f624bb84276724607"
    )
    assert load_definition_json(BASELINE.read_text()).source_hash == source_digest(source.decode())
    before, after = [json.loads(path.read_text()) for path in (BASELINE, CURRENT)]
    for body in (before, after):
        for rule in body["rules"]:
            rule.pop("source")
        for step in body["steps"].values():
            step.pop("source")
    assert after["rules"] == before["rules"]
    assert after["steps"] == before["steps"]
    assert after["compiled_against"] == before["compiled_against"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda row: row["id"])
async def test_recorded_scenarios_are_equivalent(scenario, monkeypatch):
    traces = []
    for path in (BASELINE, CURRENT):
        calls = []

        async def execute(name, args):
            calls.append([name, args])
            response = scenario["responses"][name]
            if response == "runtime_error":
                raise RuntimeError("injected domain failure")
            return response

        handler = SimpleNamespace(execute=execute)
        monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
        engine, runs = engine_for(path, handler)
        result = await engine.dispatch_event(scenario["event"], TRUSTED_LOCAL)
        assert list(result.rules_selected) == scenario["rules"]
        assert calls == scenario["calls"], (path, runs.receipts)
        snapshots = [await runs.load_run(run_id) for run_id in result.run_ids]
        assert [snapshot.lifecycle.value for snapshot in snapshots] == scenario["lifecycles"]
        # An identical delivery reuses the run without another domain invocation.
        receipts = list(runs.receipts)
        replay = await engine.dispatch_event(scenario["event"], TRUSTED_LOCAL)
        assert replay.run_ids == result.run_ids
        assert calls == scenario["calls"]
        assert runs.receipts == receipts
        # Command order, inputs, typed bindings, outcomes, attempts and terminals
        # must match; new hashes and source line references intentionally differ.
        traces.append((
            calls,
            [(s.lifecycle, s.bindings) for s in snapshots],
            [(r.step_id, r.outcome, r.attempt) for r in runs.receipts],
        ))
    assert traces[0] == traces[1]


@pytest.mark.parametrize("path", [BASELINE, CURRENT], ids=["installed", "ported"])
async def test_real_dispatch_preserves_daemon_notification_dedup(
    path, command_handler_factory, monkeypatch,
):
    handler = await command_handler_factory()
    monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
    service = handler.orchestrator.provider_availability
    service._probe_impl = AsyncMock(return_value="cannot_tell")
    service.on_half_change = None
    try:
        for _ in range(2):
            await service.record("codex", STARTUP_DIALOG, "auth", detail={"dialog": "login"})
        await service.wait_for_probes()
        # The daemon can send first; the playbook and an event replay must share
        # its durable generation deduplication rather than create another episode.
        first = await handler.execute("provider_availability_notify", {"provider": "codex"})
        assert first["outcome"] == "notified"
        assert len(first["message_ids"]) == 2
        engine, runs = engine_for(path, handler)
        for delivery in ("change", "replay"):
            result = await engine.dispatch_event({
                "event_type": "provider.state_changed", "event_id": delivery,
                "provider": "codex", "generation": first["generation"],
            }, TRUSTED_LOCAL)
            [run_id] = result.run_ids
            snapshot = await runs.load_run(run_id)
            assert snapshot.lifecycle.value == "completed", runs.receipts
            assert snapshot.bindings["notice"]["outcome"] == "already_notified"
        for kind, recipient in (("user", "dashboard"), ("session", "supervisor-global")):
            messages = await handler.db.list_messages(to_kind=kind, to_id=recipient)
            assert sum("codex" in (m.subject or "") for m in messages) == 1
    finally:
        await service.close()
        await handler.db.close()
