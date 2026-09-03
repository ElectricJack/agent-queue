"""Package 7 T-9/T-10: task admission reads active V2 artifacts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import AppConfig
from src.models import Task
from src.playbooks.definition import PlaybookDefinition, load_definition_json
from src.playbooks.manager import PlaybookManager
from src.playbooks.routing import (
    install_routing_activation_snapshot,
    refresh_routing_activation_snapshot,
    requires_routing_gate,
    uses_default_triage,
)


FIXTURE = Path("tests/fixtures/playbooks/v2/default-pipeline/artifact.json")
SHA = "sha256:" + "1" * 64


def _source() -> dict:
    return {"path": "routing.md", "start_line": 1, "end_line": 1}


def _routing_artifact(
    *,
    scope: dict | None = None,
    trigger_filter: dict | None = None,
    default_triage: bool = True,
) -> PlaybookDefinition:
    trigger = {"event_type": "task.created"}
    if trigger_filter is not None:
        trigger["filter"] = trigger_filter
    raw = {
        "schema_version": 2,
        "id": "routing-policy",
        "version": 1,
        "scope": scope or {"type": "system"},
        "purpose": "routine",
        "source_hash": "sha256:" + "2" * 64,
        "compiled_at": "2026-09-03T00:00:00Z",
        "compiler_build": "test",
        "compiled_against": {},
        "rules": [
            {
                "id": "route-created",
                "name": "Route created work",
                "trigger": trigger,
                "guard": {
                    "type": "bool",
                    "op": "not",
                    "operands": [
                        {
                            "type": "exists",
                            "value": {"type": "event_ref", "path": "task.profile_id"},
                            "mode": "present",
                        }
                    ],
                },
                "entry_step": "gate",
                "source": _source(),
            }
        ],
        "steps": {
            "gate": {
                "type": "command",
                "rule": "route-created",
                "title": "Create routing gate",
                "source": _source(),
                "command": "gate_create",
                "inputs": {
                    "project_id": {"type": "event_ref", "path": "project_id"},
                    "gate_type": {"type": "literal", "value": "routing"},
                    "waiter_task_ids": {
                        "type": "list",
                        "items": [{"type": "event_ref", "path": "task_id"}],
                    },
                },
                "transitions": {"created": "triage", "runtime_error": "done"},
            },
            "triage": {
                "type": "command",
                "rule": "route-created",
                "title": "Ensure triage",
                "source": _source(),
                "command": "ensure_task" if default_triage else "task_route",
                "inputs": (
                    {
                        "project_id": {"type": "event_ref", "path": "project_id"},
                        "profile_id": {"type": "literal", "value": "triage"},
                        "dedup_key": {"type": "literal", "value": "triage-open"},
                    }
                    if default_triage
                    else {
                        "task_id": {"type": "event_ref", "path": "task_id"},
                        "profile_id": {"type": "literal", "value": "coder"},
                    }
                ),
                "transitions": {"created": "done", "runtime_error": "done"},
            },
            "done": {
                "type": "terminal",
                "rule": "route-created",
                "title": "Done",
                "source": _source(),
                "outcome": "completed",
            },
        },
    }
    return PlaybookDefinition.model_validate(raw)


class RecordingStore:
    def __init__(self, artifacts: dict[str, PlaybookDefinition]) -> None:
        self.artifacts = artifacts
        self.loads: list[str] = []

    def load(self, artifact_sha256: str) -> PlaybookDefinition:
        self.loads.append(artifact_sha256)
        return self.artifacts[artifact_sha256]


class NoSecondConnection:
    """The admission callback must never consult storage while a write is open."""

    def __getattr__(self, name: str):
        if name in {
            "list_playbook_activations",
            "get_playbook_artifact",
            "connect",
            "immediate",
        }:
            raise AssertionError(f"routing admission opened a second connection via {name}")
        raise AttributeError(name)


def _manager(
    artifact: PlaybookDefinition | None = None,
    *,
    activation: dict | None = None,
    store: RecordingStore | None = None,
) -> tuple[PlaybookManager, RecordingStore]:
    config = AppConfig()
    config.playbooks.enabled = True
    manager = PlaybookManager(
        config=config,
        command_handler=SimpleNamespace(db=NoSecondConnection()),
    )
    store = store or RecordingStore({SHA: artifact} if artifact is not None else {})
    rows = []
    if artifact is not None or activation is not None:
        rows.append(
            {
                "playbook_id": artifact.id if artifact is not None else "routing-policy",
                "scope": "system",
                "scope_identifier": "",
                "active_artifact_sha256": SHA,
                "enabled": True,
                "health": "ready",
                **(activation or {}),
            }
        )
    install_routing_activation_snapshot(manager, rows, artifact_store=store)
    return manager, store


def _task(**updates) -> Task:
    return replace(
        Task(id="new", project_id="p", title="New", description=""),
        **updates,
    )


def test_requires_routing_gate_opens_no_second_connection():
    manager, store = _manager(_routing_artifact())

    assert requires_routing_gate(manager, _task()) is True
    assert store.loads == [SHA]


async def test_refresh_reads_activations_before_admission():
    artifact = _routing_artifact()
    store = RecordingStore({SHA: artifact})
    config = AppConfig()
    config.playbooks.enabled = True
    manager = PlaybookManager(config=config)

    class ActivationSource:
        calls = 0

        async def list_playbook_activations(self, *, enabled_only=False):
            assert enabled_only is True
            self.calls += 1
            return [
                {
                    "playbook_id": artifact.id,
                    "scope": "system",
                    "scope_identifier": "",
                    "active_artifact_sha256": SHA,
                    "enabled": True,
                    "health": "ready",
                }
            ]

    source = ActivationSource()
    await refresh_routing_activation_snapshot(manager, source, artifact_store=store)

    assert source.calls == 1
    assert requires_routing_gate(manager, _task()) is True
    assert store.loads == [SHA]


async def test_refresh_failure_installs_fail_closed_snapshot():
    config = AppConfig()
    config.playbooks.enabled = True
    manager = PlaybookManager(config=config)
    store = RecordingStore({})

    class BrokenActivationSource:
        async def list_playbook_activations(self, *, enabled_only=False):
            raise RuntimeError("database offline")

    await refresh_routing_activation_snapshot(
        manager,
        BrokenActivationSource(),
        artifact_store=store,
    )

    assert requires_routing_gate(manager, _task()) is True
    assert uses_default_triage(manager, "p") is False


@pytest.mark.parametrize(
    ("case", "manager_factory", "task", "extra", "expected_gate", "expected_triage"),
    [
        ("routing rule", lambda: _manager(_routing_artifact())[0], _task(), None, True, True),
        (
            "profiled task",
            lambda: _manager(_routing_artifact())[0],
            _task(profile_id="coder"),
            None,
            False,
            True,
        ),
        (
            "filter miss",
            lambda: _manager(_routing_artifact(trigger_filter={"task_type": "feature"}))[0],
            _task(),
            None,
            False,
            True,
        ),
        (
            "filter match",
            lambda: _manager(_routing_artifact(trigger_filter={"task_type": "feature"}))[0],
            _task(),
            {"task_type": "feature"},
            True,
            True,
        ),
        (
            "valid artifact without routing rule",
            lambda: _manager(load_definition_json(FIXTURE.read_text()))[0],
            _task(),
            None,
            False,
            False,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_routing_admission_parity_with_v1_cases(
    case, manager_factory, task, extra, expected_gate, expected_triage
):
    manager = manager_factory()

    assert requires_routing_gate(manager, task, extra) is expected_gate, case
    assert uses_default_triage(manager, task.project_id) is expected_triage, case


def test_project_activation_only_applies_to_its_project():
    artifact = _routing_artifact(scope={"type": "project", "project_id": "p"})
    manager, _ = _manager(
        artifact,
        activation={"scope": "project", "scope_identifier": "p"},
    )

    assert requires_routing_gate(manager, _task()) is True
    assert requires_routing_gate(manager, _task(project_id="elsewhere")) is True
    assert uses_default_triage(manager, "p") is True
    assert uses_default_triage(manager, "elsewhere") is False


def test_no_activation_fails_closed_for_gate_but_not_triage():
    manager, _ = _manager()

    assert requires_routing_gate(manager, _task()) is True
    assert uses_default_triage(manager, "p") is False


def test_unloadable_artifact_fails_closed_for_gate_but_not_triage():
    manager, store = _manager(
        activation={
            "playbook_id": "routing-policy",
            "active_artifact_sha256": SHA,
        }
    )

    assert requires_routing_gate(manager, _task()) is True
    assert uses_default_triage(manager, "p") is False
    assert store.loads == [SHA, SHA]


def test_disabled_playbook_subsystem_does_not_invent_policy():
    manager, store = _manager(_routing_artifact())
    manager._config.playbooks.enabled = False

    assert requires_routing_gate(manager, _task()) is False
    assert uses_default_triage(manager, "p") is False
    assert store.loads == []
