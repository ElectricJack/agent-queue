"""Event field descriptions are additive to emission validation."""

from src.event_schemas import (
    CONTRACTED_EVENT_TYPES,
    EVENT_SCHEMAS,
    event_field_is_sensitive,
    resolve_event_path,
    validate_event,
)


def test_hydrated_task_fields_are_resolvable_but_not_emitter_fields() -> None:
    assert resolve_event_path("task.completed", "task.branch_name") is not None
    assert validate_event(
        "task.completed",
        {"task_id": "t", "project_id": "p", "title": "T", "task": {}},
        strict_extras=True,
    ) == ["[task.completed] unexpected field 'task'"]


def test_event_sensitivity_inherits_from_ancestors(monkeypatch) -> None:
    from src import event_schemas

    monkeypatch.setitem(
        event_schemas.EVENT_SCHEMAS,
        "example.sensitive",
        {"required": [], "optional": [], "fields": {"secret": {"type": "object", "description": "secret", "sensitive": True, "fields": {"value": {"type": "string", "description": "value"}}}}},
    )
    assert event_field_is_sensitive("example.sensitive", "secret.value")


def test_contracted_events_describe_every_emitted_field_and_playbook_path() -> None:
    for event_type in CONTRACTED_EVENT_TYPES:
        schema = EVENT_SCHEMAS[event_type]
        for field in [*schema["required"], *schema["optional"]]:
            assert schema["fields"][field]["description"]
    for event_type, path in (
        ("task.completed", "project_id"),
        ("task.completed", "task_id"),
        ("task.completed", "title"),
        ("task.completed", "task.branch_name"),
        ("task.completed", "task.pr_url"),
        ("spec.approved", "spec_path"),
        ("proposal.ready", "proposal_id"),
        ("gate.resolved", "await_id"),
    ):
        assert resolve_event_path(event_type, path) is not None
