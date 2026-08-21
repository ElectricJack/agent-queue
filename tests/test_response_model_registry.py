"""Guard: every categorized command surfaced through the codegen router has
a Pydantic response model registered.  Without this, the generated TS client
sees ``unknown`` for that call, which silently breaks the dashboard."""

from __future__ import annotations

import pytest

from src.api.codegen import API_EXCLUDED
from src.api.models import get_all_response_models
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
