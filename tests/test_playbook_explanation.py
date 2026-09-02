"""Intent is a pure projection of the contract that a node would invoke."""

from src.commands.contracts import CONTRACTS
from src.playbooks.explanation import REDACTED, render_node_explanation


def _node(args: dict) -> dict:
    return {
        "action": {
            "command": "ensure_task",
            "args": args,
            "on_success": "linked",
            "on_failure": "done",
            "output": {"as": "review"},
        }
    }


def test_explanation_uses_registered_contract_and_never_executes_it() -> None:
    explanation = render_node_explanation(
        "create",
        _node(
            {
                "project_id": "{{event.project_id}}",
                "dedup_key": "review:{{event.task_id}}",
                "title": "Review",
            }
        ),
        event_type="task.completed",
    )
    assert explanation is not None
    assert explanation.contract_fingerprint == CONTRACTS.fingerprint("ensure_task")
    assert explanation.effects[0].text == 'Create or reuse task keyed by "dedup_key"'
    assert explanation.inputs[0].value.text == "this event's the project the task belongs to"
    assert {item.target_node_id for item in explanation.outcomes} == {"linked", "done"}
    assert explanation.result and explanation.result.fields == ["task_id", "created"]


def test_unknown_fields_are_visible_and_uncontracted_nodes_remain_legacy() -> None:
    explanation = render_node_explanation(
        "create", _node({"dedup_key": "x", "title": "T", "future": 1})
    )
    assert explanation is not None and explanation.unrendered_fields == ["future"]
    assert render_node_explanation("unknown", {"action": {"command": "other", "args": {}}}) is None


def test_event_sensitive_value_is_redacted(monkeypatch) -> None:
    from src import event_schemas

    monkeypatch.setitem(
        event_schemas.EVENT_SCHEMAS,
        "secret.event",
        {
            "required": [],
            "optional": [],
            "fields": {"token": {"type": "string", "description": "token", "sensitive": True}},
        },
    )
    explanation = render_node_explanation(
        "create", _node({"dedup_key": "{{event.token}}", "title": "T"}), event_type="secret.event"
    )
    assert explanation is not None
    value = explanation.inputs[0].value
    assert value.text == REDACTED and value.redacted and value.raw is None


def test_every_registered_effect_is_renderable() -> None:
    for name in CONTRACTS.names():
        for clause in CONTRACTS.require(name).contract.execution.effects:
            explanation = render_node_explanation(
                name, {"action": {"command": name, "args": {}}}
            )
            assert explanation is not None
