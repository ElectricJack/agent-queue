"""Typed condition boundaries; no source or sleep policy is accepted as prose."""

import ast
from pathlib import Path

import pytest

from src.waits.conditions import Condition, SourceEvent, WaitError
from src.waits.registry import build_registry

NOW = 1_800_000_000.0
SHA = "a" * 40

CONDITIONS = [
    {"kind": "task", "task_id": "t"},
    {"kind": "dependency", "task_id": "t", "depends_on_task_id": "b"},
    {"kind": "job", "job_id": "j", "attempt": 1},
    {"kind": "ci_check", "repository_id": "r", "head_sha": SHA, "check_name": "tests"},
    {"kind": "ci_run", "repository_id": "r", "head_sha": SHA, "run_id": "123"},
    {"kind": "pull_request", "repository_id": "r", "expected_head_sha": SHA, "pr_number": 3},
    {"kind": "batch", "batch_id": "b"},
    {"kind": "message", "thread_id": "thread", "after_seq": 0},
    {"kind": "human", "subject": "review", "review_id": "rev"},
    {"kind": "human", "subject": "task_comment", "task_id": "t"},
    {"kind": "human", "subject": "message_thread", "thread_id": "thread", "after_seq": 0},
    {"kind": "timer", "due_at": NOW + 20},
    {"kind": "resource", "blocker": "slot", "identity": "p:profile", "source_revision": "1"},
    {"kind": "playbook_condition", "run_id": "run", "wait_id": "wait"},
]


@pytest.mark.parametrize("raw", CONDITIONS)
def test_normalised_conditions_round_trip_without_trusting_internal_bindings(raw):
    registry = build_registry(clock=lambda: NOW)
    cond = registry.normalise(raw)
    assert registry.normalise(cond.as_dict()) == cond
    bound = cond.bind(generation=7, project_id="p", blockers=[])
    assert Condition.from_record(bound.as_record()) == bound
    with pytest.raises(WaitError, match="typed condition"):
        registry.normalise(bound.as_record())


@pytest.mark.parametrize(
    "extra",
    [
        {"any": []},
        {"all": []},
        {"condition": {}},
        {"timeout": 30},
        {"mode": "in_place"},
        {"claimed": "CI passed"},
        {"head_sha": SHA},
    ],
)
@pytest.mark.parametrize("raw", CONDITIONS)
def test_mixed_predicates_and_agent_policy_are_refused(raw, extra):
    # head_sha is an identity field on CI, not an extra predicate there.
    if set(extra) <= set(raw):
        return
    with pytest.raises(WaitError) as error:
        build_registry().normalise({**raw, **extra})
    assert error.value.code == "wait.condition_required"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "when it is done",
        {},
        {"kind": []},
        {"kind": "review", "review_id": "r"},
        {"kind": "task", "task_id": "t", "generation": True},
        {"kind": "task", "task_id": "t", "target_statuses": "COMPLETED"},
        {"kind": "task", "task_id": "t", "target_statuses": []},
        {"kind": "task", "task_id": "t", "target_statuses": ["UNKNOWN"]},
        {"kind": "task", "task_id": "t", "target_statuses": ["CANCELLED"]},
        {"kind": "task", "task_id": False},
        {"kind": "dependency", "task_id": "a", "depends_on_task_id": "b", "dep_type": []},
        {"kind": "timer", "due_at": float("nan")},
        {"kind": "timer", "due_at": float("inf")},
        {"kind": "timer", "due_at": "2026-01-01T00:00:00"},
        {"kind": "ci_check", "repository_id": "r", "head_sha": "abc123", "check_name": "tests"},
        {"kind": "message", "thread_id": "t", "after_seq": -1},
        {"kind": "job", "job_id": "j", "attempt": 0},
        {"kind": "batch", "batch_id": "b", "predicate": "green"},
        {"kind": "resource", "blocker": "anything", "identity": "i", "source_revision": "v"},
    ],
)
def test_invalid_conditions_have_a_structured_refusal(raw):
    with pytest.raises(WaitError) as error:
        build_registry().normalise(raw)
    assert error.value.as_dict()["code"] == "wait.condition_required"


def test_registry_is_frozen_and_has_exactly_the_approved_kinds():
    registry = build_registry()
    assert set(registry.adapters) == {c["kind"] for c in CONDITIONS}
    with pytest.raises(TypeError):
        registry.adapters["unchecked"] = object()


def test_result_projection_rejects_a_new_source_generation():
    registry = build_registry()
    adapter = registry.adapter("task")
    old = registry.normalise({"kind": "task", "task_id": "t", "generation": 1})
    new = registry.normalise({"kind": "task", "task_id": "t", "generation": 2})
    event = SourceEvent(adapter.source_key(new), "rev", "result:2")
    with pytest.raises(WaitError):
        adapter.project_result(old, event)
    assert adapter.project_result(new, event) == "result:2"


@pytest.mark.parametrize(
    "raw,identity,generation",
    list(
        zip(
            CONDITIONS,
            [
                "task:t",
                "dependency:t:b:blocks",
                "job:j",
                f"ci_check:r:{SHA}:tests",
                f"ci_run:r:{SHA}:123",
                f"pull_request:r:3:{SHA}",
                "batch:b",
                "message:thread",
                "review:rev",
                "human:task_comment:t",
                "message:thread",
                f"timer:{NOW + 20}",
                "resource:slot:p:profile",
                "playbook_condition:run:wait",
            ],
            [7, 7, 7, 0, 0, 0, 7, 0, 7, 7, 0, 0, 0, 7],
            strict=True,
        )
    ),
)
def test_source_identity_contract_for_event_writers(raw, identity, generation):
    registry = build_registry(clock=lambda: NOW)
    cond = registry.normalise(raw).bind(generation=7)
    source = registry.adapter(cond.kind).source_key(cond)
    assert (source.identity, source.generation) == (identity, generation)


def test_wait_package_has_no_imports_from_command_session_or_orchestrator_packages():
    for path in Path("src/waits").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert not any(
                name.startswith(("src.commands", "src.sessions", "src.orchestrator"))
                for name in names
            ), path
