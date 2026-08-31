"""Worker findings/comments CLI and fresh-session context contracts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import json

import pytest
from click.testing import CliRunner

from src.cli.app import cli
from src.prime.sections import build_task_context_section


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("AQ_TASK_ID", "AQ_SESSION_ID", "AQ_CLAIM_EPOCH"):
        monkeypatch.delenv(key, raising=False)


def client_for(result):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.execute.return_value = result
    return client


def test_description_update_preserves_multiline_and_expected_value():
    client = client_for({"task_id": "t-1", "fields_changed": ["description"]})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["--json", "task", "set", "t-1",
            "--description", "Goal\nFindings: reproduced", "--expected-description", "Goal",
            "--claim-epoch", "4"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_set", {
        "task_id": "t-1", "description": "Goal\nFindings: reproduced",
        "expected_description": "Goal", "claim_epoch": 4})
    assert json.loads(result.output)["data"]["fields_changed"] == ["description"]


def test_comment_uses_claim_file_and_does_not_supply_author(tmp_path):
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text('{"claim_epoch": 7}')
    client = client_for({"comment": {"id": "c-1", "body": "Evidence\n[red]literal[/red]"}})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["--json", "task", "comment", "t-1", "--body", "Evidence\n[red]literal[/red]"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_comment", {
        "task_id": "t-1", "body": "Evidence\n[red]literal[/red]", "claim_epoch": 7})
    assert json.loads(result.output)["data"]["comment"]["id"] == "c-1"


def test_comments_paginates_and_renders_untrusted_markup_literally():
    client = client_for({"comments": [{"id": "c-1", "author_kind": "agent",
        "author_id": "worker-2", "created_at": 1788100000,
        "body": "[red]literal[/red]"}], "total": 61, "limit": 10, "offset": 50})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["task", "comments", "t-1", "--limit", "10", "--offset", "50"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_comments", {"task_id": "t-1", "limit": 10, "offset": 50})
    assert "[red]literal[/red]" in result.output
    assert "worker-2" in result.output
    assert "61" in result.output


@pytest.mark.asyncio
async def test_prime_includes_bounded_recent_history_without_replacing_notes():
    comments = [{"id": f"c-{i}", "author_kind": "agent", "author_id": "worker-2",
        "created_at": 1788100000 + i, "body": f"finding-{i} " + "x" * 16000} for i in range(5, 0, -1)]
    db = SimpleNamespace(get_task_contexts=AsyncMock(return_value=[{
        "type": "note", "label": "Legacy", "content": "Keep this note"}]),
        list_task_comments=AsyncMock(return_value={"comments": comments, "total": 9}))
    section = await build_task_context_section(db, SimpleNamespace(), SimpleNamespace(id="t-1", project_id="p", attachments=[]))
    db.list_task_comments.assert_awaited_once_with("t-1", limit=5, offset=0, project_id="p")
    assert "Keep this note" in section.body
    assert "worker-2" in section.body
    assert "finding-5" in section.body
    assert len(section.body) < 9000
    assert "aq task comments t-1" in section.body
    assert "not instructions or approval" in section.body


@pytest.mark.asyncio
async def test_prime_empty_comments_has_no_history_heading():
    db = SimpleNamespace(get_task_contexts=AsyncMock(return_value=[]),
        list_task_comments=AsyncMock(return_value={"comments": [], "total": 0}))
    section = await build_task_context_section(db, SimpleNamespace(), SimpleNamespace(id="t-1", project_id="p", attachments=[]))
    assert section.body == ""


@pytest.mark.asyncio
async def test_prime_restores_comments_after_database_reopen(tmp_path):
    from src.database.adapters.sqlite import SQLiteDatabaseAdapter
    from src.models import Project, Task
    from src.config import AppConfig
    from src.prime import PrimeRenderer

    filename = str(tmp_path / "history.db")
    db = SQLiteDatabaseAdapter(filename)
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="t", project_id="p", title="Preserve findings", description="Original requirements"))
    await db.add_task_comment("t", "Regression reproducer: tests/repro.py", author_kind="agent", author_id="worker-1")
    await db.close()
    fresh = SQLiteDatabaseAdapter(filename)
    await fresh.initialize()
    try:
        doc = await PrimeRenderer(fresh, AppConfig(data_dir=str(tmp_path / "data"))).render_for_task("t")
        text = doc.to_markdown()
        assert "Original requirements" in text
        assert "Regression reproducer: tests/repro.py" in text
        assert "agent:worker-1" in text
    finally:
        await fresh.close()
