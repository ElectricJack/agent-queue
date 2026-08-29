from __future__ import annotations

from click.testing import CliRunner

from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES


def defs():
    return {d["name"]: d for d in _ALL_TOOL_DEFINITIONS}


def test_definitions_present():
    d = defs()
    for name in ("formula_list", "formula_show", "formula_cook"):
        assert name in d and _TOOL_CATEGORIES[name] == "formula"
    assert {"name", "project_id", "vars", "parent_id", "dry_run"} <= set(
        d["formula_cook"]["input_schema"]["properties"])
    assert {"name", "as_cooked", "vars"} <= set(d["formula_show"]["input_schema"]["properties"])


def test_agent_scope():
    from src.api.scope import AGENT_COMMAND_SET

    assert {"formula_list", "formula_show"} <= AGENT_COMMAND_SET
    assert "formula_cook" not in AGENT_COMMAND_SET


def test_cli_cook_collects_vars(monkeypatch):
    from src.cli import formulas as cli_formulas
    from src.cli.app import cli

    sent = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, command, args):
            sent.update(command=command, args=args)
            return {"success": True, "container_id": "c1", "task_ids": [], "nodes": []}

    monkeypatch.setattr(cli_formulas, "_get_client", lambda *a, **k: FakeClient())
    r = CliRunner().invoke(cli, ["formula", "cook", "review-and-fix", "-p", "p1",
                                 "--var", "branch=feat/x", "--var", "fixer=coding", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert sent["command"] == "formula_cook"
    assert sent["args"]["vars"] == {"branch": "feat/x", "fixer": "coding"}
    assert sent["args"]["dry_run"] is True


def test_response_models_registered():
    from src.api.models.task import RESPONSE_MODELS

    for name in ("formula_list", "formula_show", "formula_cook"):
        assert name in RESPONSE_MODELS
