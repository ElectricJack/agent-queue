"""Guard: every categorized command surfaced through the codegen router has
a Pydantic response model registered.  Without this, the generated TS client
sees ``unknown`` for that call, which silently breaks the dashboard."""

from __future__ import annotations

import pytest

from src.api.codegen import API_EXCLUDED, _CODEGEN_INPUT_SCHEMAS, _make_input_model
from src.api.models import get_all_response_models
from src.api.models.message import MessageModel
from src.api.models.session import SessionSummary
from src.tools import _CLI_CATEGORY_OVERRIDES, _TOOL_CATEGORIES

# Commands that intentionally return an unstructured dict (extra="allow") and
# are declared with model_config={"extra": "allow"} elsewhere.  Add here only
# with a code comment justifying why.
_UNSTRUCTURED_EXEMPT: set[str] = {
    # Pre-existing gaps outside the Wave-4 lane-A scope: these commands were
    # already categorized without a response model before this test landed.
    # Leaving them exempt keeps the guard useful for new additions (any new
    # categorized command without a model still fails) without demanding a
    # cleanup pass that isn't part of this lane.
    "ask_human",
    "create_task_graph",
    "db_preflight_hierarchy",
    "doctor",
    "get_chat_analyzer_metrics",
    "get_costs",
    "get_schema",
    "list_workspace_kinds",
    "memory_save",
    "task_close",
    "task_heartbeat",
    "task_set",
    "task_show",
    "workspace_doctor",
    "workspace_reap",
}


@pytest.mark.parametrize(
    "cmd_name",
    sorted(
        ({name for name, _cat in _TOOL_CATEGORIES.items()} | set(_CLI_CATEGORY_OVERRIDES))
        - API_EXCLUDED
        - _UNSTRUCTURED_EXEMPT
    ),
)
def test_every_categorized_command_has_response_model(cmd_name: str) -> None:
    models = get_all_response_models()
    assert cmd_name in models, (
        f"Command '{cmd_name}' is categorized (auto-generates a REST route) "
        "but has no entry in any src/api/models/*.py RESPONSE_MODELS dict. "
        "Add one so the generated TS client has a concrete type."
    )


def test_message_model_from_alias_round_trips() -> None:
    """``MessageModel.from_`` must round-trip through the ``"from"`` JSON key.

    ``message_to_dict`` emits ``"from": "kind:id"``; without a Pydantic alias
    the value is silently dropped on validation and re-serialized as ``"from_"``,
    breaking the generated TS client and any caller that reads the field.
    """
    raw = {
        "id": "msg-1",
        "project_id": "proj-1",
        "from_kind": "user",
        "from_id": "alice",
        "from": "user:alice",
        "to_kind": "session",
        "to_id": "sess-1",
        "to": "session:sess-1",
        "body": "hello",
    }
    model = MessageModel.model_validate(raw)
    assert model.from_ == "user:alice", (
        "MessageModel did not populate from_ from the 'from' key in the input dict"
    )
    serialized = model.model_dump(by_alias=True)
    assert serialized.get("from") == "user:alice", (
        "MessageModel serialized 'from_' instead of 'from' — alias missing or wrong"
    )
    assert "from_" not in serialized, "Serialized output must use the 'from' key, not 'from_'"


# ---------------------------------------------------------------------------
# Guard against the empty-schema silent-drop defect fixed in this commit.
# See src/api/codegen.py::_CODEGEN_INPUT_SCHEMAS for the underlying story.
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS_BY_COMMAND: dict[str, set[str]] = {
    # explain + ready frontier
    "explain_task": {"task_id"},
    # project_ready has no required args (project_id falls back to active)
    "project_ready": set(),
    # gate operator surface
    "gate_create": {"project_id", "gate_type", "title"},
    "gate_list": set(),
    "gate_show": {"gate_id"},
    "gate_resolve": {"gate_id", "resolved_by"},
    # session operator surface — identifier args are optional at the schema
    # layer because ``_resolve_session`` accepts any of session_id/id/name/
    # task_id; the presence of the property is what matters for silent-drop.
    "session_list": set(),
    "session_show": set(),
    "session_peek": set(),
    "session_attach": set(),
    "session_nudge": set(),
    "session_logs": set(),
    "session_kill": set(),
}


_PRESENT_PROPERTIES_BY_COMMAND: dict[str, set[str]] = {
    "explain_task": {"task_id"},
    "project_ready": {"project_id", "labels", "any_label", "profile_id", "brief"},
    "gate_create": {"project_id", "gate_type", "title"},
    "gate_show": {"gate_id"},
    "gate_resolve": {"gate_id", "resolved_by"},
    # each session command exposes an identifier + kind-specific extras
    "session_show": {"session_id", "name", "task_id"},
    "session_peek": {"session_id", "name", "task_id"},
    "session_attach": {"session_id", "name", "task_id"},
    "session_nudge": {"session_id", "name", "task_id", "text"},
    "session_logs": {"session_id", "name", "task_id"},
    "session_kill": {"session_id", "name", "task_id"},
}


def _tool_input_schema(cmd_name: str) -> dict:
    from src.tools import _ALL_TOOL_DEFINITIONS

    for defn in _ALL_TOOL_DEFINITIONS:
        if defn["name"] == cmd_name:
            return defn["input_schema"]
    raise AssertionError(f"{cmd_name} has neither a codegen override nor a tool definition")


@pytest.mark.parametrize("cmd_name", sorted(_REQUIRED_FIELDS_BY_COMMAND))
def test_codegen_request_model_has_expected_fields(cmd_name: str) -> None:
    """The codegen request model must expose the properties the ``_cmd_*``
    method actually reads from its ``args`` dict.  Without this the FastAPI
    body model silently drops client fields — see the ``_CODEGEN_INPUT_SCHEMAS``
    docstring for the reproducer that motivated this guard.

    The schema comes from the codegen override when there is one, else from
    the command's real ``_ALL_TOOL_DEFINITIONS`` entry — ``project_ready``
    graduated to a full tool definition (so the CLI exposes it) and no longer
    needs an override.
    """
    schema = _CODEGEN_INPUT_SCHEMAS.get(cmd_name) or _tool_input_schema(cmd_name)
    model = _make_input_model(cmd_name, schema)
    fields = set(model.model_fields.keys())
    required = {name for name, field in model.model_fields.items() if field.is_required()}

    assert _REQUIRED_FIELDS_BY_COMMAND[cmd_name] <= required, (
        f"{cmd_name}: expected required fields "
        f"{_REQUIRED_FIELDS_BY_COMMAND[cmd_name]} but model requires {required}"
    )
    expected_present = _PRESENT_PROPERTIES_BY_COMMAND.get(cmd_name, set())
    assert expected_present <= fields, (
        f"{cmd_name}: expected properties {expected_present} to appear on the "
        f"request model but only found {fields}"
    )


def test_session_summary_accepts_hex_string_epoch() -> None:
    """``sessions.epoch`` is a Text column carrying values like
    ``"5b8c0ab48772"`` — the model must not coerce it to int."""
    row = {
        "id": "sess-1",
        "name": "s-task-1",
        "task_id": "task-1",
        "project_id": "proj-1",
        "profile_id": "profile-1",
        "harness": "claude",
        "provider": "tmux",
        "lifecycle": "task",
        "state": "running",
        "work_dir": "/tmp/work",
        "started_at": 1_700_000_000.0,
        "last_activity": 1_700_000_050.0,
        "restarts": 0,
        "quarantined_at": None,
        "sleep_reason": None,
        "epoch": "5b8c0ab48772",
        "idle_seconds": 12.5,
        "stalled": False,
    }
    model = SessionSummary.model_validate(row)
    assert model.epoch == "5b8c0ab48772"
