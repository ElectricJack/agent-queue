"""Generated surface for the swarm commands — spec §14."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project
from src.orchestrator import Orchestrator
from src.tools.definitions import _ALL_TOOL_DEFINITIONS

PROJECT_ID = "proj"


def defs():
    return {d["name"]: d for d in _ALL_TOOL_DEFINITIONS}


def test_task_claim_definition():
    d = defs()["task_claim"]
    assert set(d["input_schema"]["properties"]) >= {"task_id", "next", "wait"}
    assert d.get("category") == "tasks"


def test_close_and_mutators_carry_claim_epoch():
    d = defs()
    for name in ("task_close", "task_heartbeat", "task_set", "task_handoff"):
        assert "claim_epoch" in d[name]["input_schema"]["properties"], name
    assert {"claim_next", "wait"} <= set(d["task_close"]["input_schema"]["properties"])


def test_create_task_accepts_swarm_fields():
    props = defs()["create_task"]["input_schema"]["properties"]
    assert {"depends_on", "discovered_from", "dedup_key", "parent_id"} <= set(props)


def test_pool_commands_defined():
    d = defs()
    assert d["pool_status"]["category"] == "ops"
    assert {"project_id", "profile_id", "min", "max", "now"} <= set(
        d["pool_scale"]["input_schema"]["properties"]
    )


def test_read_claim_epoch_prefers_file(tmp_path, monkeypatch):
    from src.cli.agent_surface import read_claim_epoch

    monkeypatch.setenv("AQ_CLAIM_EPOCH", "9")
    assert read_claim_epoch(str(tmp_path)) == 9
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({"task_id": "t", "claim_epoch": 3}))
    assert read_claim_epoch(str(tmp_path)) == 3


def test_cli_close_sends_claim_epoch(tmp_path, monkeypatch):
    from src.cli import agent_surface
    from src.cli.app import cli

    sent = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, command, args=None):
            sent.update(command=command, args=args or {})
            return {"success": True}

    monkeypatch.setattr(agent_surface, "_get_client", lambda *a, **k: FakeClient())
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({"task_id": "t1", "claim_epoch": 4}))
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(
        cli, ["task", "close", "t1", "--outcome", "pass", "--summary", "s", "--claim-next"]
    )
    assert r.exit_code == 0, r.output
    assert sent["command"] == "task_close"
    assert (sent["args"]["claim_epoch"], sent["args"]["claim_next"]) == (4, True)


def test_cli_claim_timeout_is_long():
    from src.cli.client import _COMMAND_TIMEOUTS

    assert _COMMAND_TIMEOUTS["task_claim"] >= 180 and _COMMAND_TIMEOUTS["task_close"] >= 180


@pytest.fixture
async def handler(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    await db.create_project(Project(id=PROJECT_ID, name="p"))
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
    yield CommandHandler(orch, cfg)
    await db.close()


async def test_schema_enums(handler):
    res = await handler._cmd_get_schema({})
    enums = res["enums"]
    assert enums["lifecycle"] == ["task", "named", "pool"]
    assert "stale_claim" in enums["claim_result"] and enums["outcome"] == ["pass", "fail"]


async def test_pool_status_empty(handler):
    res = await handler._cmd_pool_status({})
    assert res == {"success": True, "pools": []}


def test_skill_documents_worker_loop():
    text = open("src/skills/aq-tasks/SKILL.md", encoding="utf-8").read()
    assert "aq task claim --next" in text and "--claim-next" in text
    assert "--outcome pass|fail" in text and "needs_context" not in text
