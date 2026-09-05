"""Package 2's review-only command/API/tool surface."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.commands.handler import CommandHandler
from src.commands.playbook_v2_commands import (
    PLAYBOOK_V2_COMPILER_COMMANDS,
    V2_COMPILER_DISABLED_ERROR,
)
from src.playbooks.authoring import PlaybookSource
from src.playbooks.pipeline_lowering import lower_pipeline
from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES
from tests.playbook_v2_helpers import twin

RESPONSE_FIELDS = json.loads(
    Path("tests/fixtures/playbooks/v2/command-response-fields.json").read_text()
)


@dataclass
class _Playbooks:
    enabled: bool = True
    v2_compiler_enabled: bool = True
    v2_api: bool = False
    v2_activation_writes: bool = False


def _handler(tmp_path: Path, *, enabled: bool = True) -> CommandHandler:
    orchestrator = MagicMock()
    orchestrator.db = AsyncMock()
    orchestrator.db.list_profiles = AsyncMock(return_value=[])
    config = MagicMock()
    config.data_dir = str(tmp_path)
    config.vault_root = str(tmp_path / "vault")
    config.playbooks = _Playbooks(enabled=enabled)
    return CommandHandler(orchestrator, config)


def _copy_source(tmp_path: Path, name: str) -> Path:
    target = tmp_path / "vault" / "system" / "playbooks" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path("src/prompts/default_playbooks") / name
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _copy_frozen_v1_pipeline(tmp_path: Path) -> Path:
    """A vault copy of the pre-Package-6 `default-pipeline.md`.

    The lowering assertions below are about how a *machine graph* maps to
    source lines, and the shipped Markdown is a prose authoring source now.
    The frozen snapshot is byte-identical to the file those line numbers were
    recorded against — see `tests/fixtures/playbooks/v1/README.md`.
    """
    target = tmp_path / "vault" / "system" / "playbooks" / "default-pipeline.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    source = Path("tests/fixtures/playbooks/v1/default-pipeline.md")
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_all_three_commands_have_tool_and_category_entries():
    definitions = {definition["name"] for definition in _ALL_TOOL_DEFINITIONS}
    assert PLAYBOOK_V2_COMPILER_COMMANDS <= definitions
    assert all(_TOOL_CATEGORIES[name] == "playbook" for name in PLAYBOOK_V2_COMPILER_COMMANDS)


async def test_every_command_is_disabled_by_default(tmp_path):
    handler = _handler(tmp_path, enabled=False)
    for name, args in {
        "playbook_v2_validate": {"path": "artifact.json"},
        "playbook_v2_propose": {"playbook_id": "p", "semantic_body_path": "body.json"},
        "playbook_v2_shadow_compile": {},
    }.items():
        result = await getattr(handler, f"_cmd_{name}")(args)
        assert result == {"success": False, "error": V2_COMPILER_DISABLED_ERROR}


async def test_validate_returns_the_pinned_response_shape(tmp_path):
    artifact = tmp_path / "vault" / "artifact.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps(twin()), encoding="utf-8")
    result = await _handler(tmp_path)._cmd_playbook_v2_validate({"path": "artifact.json"})
    assert set(result) == set(RESPONSE_FIELDS["validate"])
    assert set(result["counts"]) == {"error", "warning", "question", "info"}


async def test_propose_returns_review_material_without_installing(tmp_path):
    source = tmp_path / "vault" / "system" / "playbooks" / "demo.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nid: demo\nscope: system\ntriggers: [task.completed]\n---\n"
        "Use `demo_command`, `project_id`, `done`, `worker`, `task_id`, and `review`.\n",
        encoding="utf-8",
    )
    body_path = tmp_path / "vault" / "demo.body.json"
    body = twin()
    body_path.write_text(
        json.dumps({"rules": body["rules"], "steps": body["steps"]}), encoding="utf-8"
    )
    handler = _handler(tmp_path)
    handler.orchestrator.playbook_manager = MagicMock()
    result = await handler._cmd_playbook_v2_propose(
        {"playbook_id": "demo", "semantic_body_path": "demo.body.json"}
    )
    assert result["success"] is True
    assert result["artifact"]["id"] == "demo"
    assert set(result) == set(RESPONSE_FIELDS["propose"])
    handler.orchestrator.playbook_manager.assert_not_called()


async def test_shadow_compile_reports_every_vault_source_and_writes_nothing(tmp_path):
    pipeline = _copy_frozen_v1_pipeline(tmp_path)
    prose = tmp_path / "vault" / "system" / "playbooks" / "prose.md"
    prose.write_text(
        "---\nid: prose\nscope: system\ntriggers: [task.completed]\n---\nNeeds a proposal.\n",
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in (pipeline, prose)}
    result = await _handler(tmp_path)._cmd_playbook_v2_shadow_compile({"scope": "system"})
    assert result["success"] is True
    assert set(result) == set(RESPONSE_FIELDS["shadow_compile"])
    assert result["total"] == 2
    assert result["lowered"] == 1
    pipeline_row = next(row for row in result["rows"] if row["playbook_id"] == "default-pipeline")
    assert pipeline_row["counts"] == {"error": 0, "warning": 0, "question": 0, "info": 0}
    assert pipeline_row["artifact_sha256"].startswith("sha256:")
    assert before == {path: path.read_bytes() for path in (pipeline, prose)}


def test_default_pipeline_source_refs_point_to_exact_json_key_lines(tmp_path):
    path = _copy_frozen_v1_pipeline(tmp_path)
    source = PlaybookSource.load(path, vault_root=tmp_path / "vault")
    assert isinstance(source, PlaybookSource)
    body, diagnostics = lower_pipeline(source)
    assert diagnostics == []
    lines = path.read_text(encoding="utf-8").splitlines()
    for step_id, key in {
        "per-task-review--create-review": "create-review",
        "per-task-review--done": "done",
    }.items():
        step = body["steps"][step_id]
        assert lines[step["source"]["start_line"] - 1].strip().startswith(f'"{key}": {{')


def test_duplicate_terminal_keys_map_to_their_owning_rule_lines(tmp_path):
    path = _copy_frozen_v1_pipeline(tmp_path)
    source = PlaybookSource.load(path, vault_root=tmp_path / "vault")
    assert isinstance(source, PlaybookSource)
    body, diagnostics = lower_pipeline(source)
    assert diagnostics == []
    assert {
        step_id: body["steps"][step_id]["source"]["start_line"]
        for step_id in (
            "per-task-review--done",
            "per-branch-final-review--done",
            "spec-ingest-on-approve--done",
            "proposal-ready-gate--done",
            "commit-on-gate-resolve--done",
        )
    } == {
        "per-task-review--done": 165,
        "per-branch-final-review--done": 237,
        "spec-ingest-on-approve--done": 257,
        "proposal-ready-gate--done": 277,
        "commit-on-gate-resolve--done": 293,
    }
