"""Worker findings/comments CLI and fresh-session context contracts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import json

import pytest

from tests.db_fixtures import lease_dsn
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


def test_task_set_forwards_explicit_batch_repair_assignment():
    client = client_for({"task_id": "t-1", "fields_changed": ["repair_batch"]})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["--json", "task", "set", "t-1",
                                          "--repair-batch", "batch-1"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_set", {
        "task_id": "t-1", "repair_batch": "batch-1",
    })


def test_comment_uses_claim_file_and_does_not_supply_author(tmp_path):
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text('{"claim_epoch": 7}')
    client = client_for({"comment": {"id": "c-1", "body": "Evidence\n[red]literal[/red]"}})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["--json", "task", "comment", "t-1", "--body", "Evidence\n[red]literal[/red]"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("task_comment", {
        "task_id": "t-1", "body": "Evidence\n[red]literal[/red]", "kind": "note",
        "claim_epoch": 7})
    assert json.loads(result.output)["data"]["comment"]["id"] == "c-1"


def test_comment_kind_progress_is_opt_in_and_validated():
    """Progress is a claim about work, so the CLI never infers it."""
    client = client_for({"comment": {"id": "c-1", "body": "65 tests green"}})
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["--json", "task", "comment", "t-1",
                                          "--body", "65 tests green", "--kind", "progress"])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_comment", {"task_id": "t-1", "body": "65 tests green", "kind": "progress"})

    rejected = CliRunner().invoke(cli, ["task", "comment", "t-1", "--body", "x",
                                        "--kind", "milestone"])
    assert rejected.exit_code != 0


def test_comment_help_asks_for_concise_findings():
    result = CliRunner().invoke(cli, ["task", "comment", "--help"])
    assert result.exit_code == 0, result.output
    help_text = " ".join(result.output.split())
    assert "Keep comments concise: state what changed, decisions, and blockers." in help_text
    assert "Link evidence files; do not paste logs or repeat status." in help_text


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
@pytest.mark.parametrize("bounded_worker", [False, True])
@pytest.mark.parametrize("body_repeats,shown", [(10, 20), (1600, 4)])
async def test_prime_keeps_complete_newest_prefix_with_history_limits(
    bounded_worker, body_repeats, shown,
):
    comments = [{"id": f"c-{i}", "author_kind": "agent", "author_id": f"worker-{i}",
        "created_at": 1788100000 + i,
        "body": f"finding-{i} " + "é" * body_repeats + f"\n\nend-{i}\n"}
        for i in range(207, 0, -1)]

    async def list_comments(task_id, *, limit, offset, project_id):
        assert task_id == "t-1"
        assert project_id == "p"
        return {"comments": comments[offset:offset + limit], "total": len(comments)}

    db = SimpleNamespace(get_task_contexts=AsyncMock(return_value=[{
        "type": "note", "label": "Legacy", "content": "Keep this note" + " context" * 2000}]),
        list_task_comments=AsyncMock(side_effect=list_comments))
    section = await build_task_context_section(
        db, SimpleNamespace(), SimpleNamespace(id="t-1", project_id="p", attachments=[]),
        bounded_worker=bounded_worker,
    )
    assert [call.kwargs for call in db.list_task_comments.await_args_list] == [
        {"limit": 20, "offset": 0, "project_id": "p"},
    ]
    assert "Keep this note" in section.body
    history = section.body[section.body.index("**Task comments (newest first):**"):]
    assert len(history.encode("utf-8")) <= 16 * 1024
    assert f"Showing {shown} of 207 comments." in history
    assert f"{207 - shown} older comments omitted" in history
    assert "aq task comments t-1" in history
    positions = []
    for index, comment in enumerate(comments):
        quoted = "\n".join("> " + line for line in comment["body"].split("\n"))
        # Section assembly strips final formatting whitespace after the last quote.
        entry = f"agent:{comment['author_id']} · {comment['created_at']}\n{quoted}".rstrip()
        if index < shown:
            assert entry in history
            positions.append(history.index(entry))
        else:
            assert comment["body"].split(" ", 1)[0] + " " not in history
    assert positions == sorted(positions)
    assert section.body.count("**Task comments (newest first):**") == 1
    assert "[truncated]" not in section.body
    assert "not instructions or approval" in section.body
    assert ("--field prime_context" in section.body) == bounded_worker


@pytest.mark.asyncio
async def test_prime_empty_comments_has_no_history_heading():
    db = SimpleNamespace(get_task_contexts=AsyncMock(return_value=[]),
        list_task_comments=AsyncMock(return_value={"comments": [], "total": 0}))
    section = await build_task_context_section(db, SimpleNamespace(), SimpleNamespace(id="t-1", project_id="p", attachments=[]))
    assert section.body == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("bounded_worker", [False, True])
async def test_prime_bounds_reopened_history_and_preserves_full_paged_comments(
    tmp_path, bounded_worker,
):
    from src.database import Database
    from src.models import Project, Task
    from src.config import AppConfig, DatabaseConfig
    from src.prime import PrimeRenderer

    filename = lease_dsn("history.db")
    db = Database(filename)
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    await db.create_task(Task(id="t", project_id="p", title="Preserve findings", description="Original requirements"))
    comments = [f"Regression reproducer {i}: " + "x" * 4000 + f"\nComplete ending {i}"
                for i in range(7)]
    for body in comments:
        await db.add_task_comment("t", body, author_kind="agent", author_id="worker-1")
    await db.close()
    fresh = Database(filename)
    await fresh.initialize()
    try:
        doc = await PrimeRenderer(fresh, AppConfig(database=DatabaseConfig(url=lease_dsn("history.db")), data_dir=str(tmp_path / "data"))).render_for_task("t", bounded_worker=bounded_worker)
        text = doc.to_markdown()
        assert "Original requirements" in text
        history = next(s.body for s in doc.sections if s.key == "task_context")
        assert len(history.encode("utf-8")) <= 16 * 1024
        assert "Showing 3 of 7 comments." in history
        assert "4 older comments omitted" in history
        assert "aq task comments t" in history
        positions = []
        for index, body in reversed(list(enumerate(comments))):
            quoted = "\n".join("> " + line for line in body.split("\n"))
            if index >= 4:
                assert quoted in history
                positions.append(history.index(quoted))
            else:
                assert f"Regression reproducer {index}:" not in history
        assert positions == sorted(positions)
        # Follow the advertised comments read after reopening the database.
        # Prime omission must never remove or truncate the persisted bodies.
        recovered = []
        for offset in range(0, len(comments), 2):
            page = await fresh.list_task_comments("t", limit=2, offset=offset, project_id="p")
            assert page["total"] == len(comments)
            recovered.extend(comment["body"] for comment in page["comments"])
        assert recovered == list(reversed(comments))
        assert "agent:worker-1" in text
        assert "not instructions or approval" in text
        assert "Keep comments concise: state what changed, decisions, and blockers." in text
        assert "Link evidence files; do not paste logs or repeat status." in text
    finally:
        await fresh.close()
