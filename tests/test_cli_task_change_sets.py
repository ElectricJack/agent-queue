"""The file CLI preserves the reviewed change set and offers an unsaved preview."""

import json
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner
import pytest

from src.cli.app import cli


@pytest.fixture(autouse=True)
def clean_worker_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("AQ_TASK_ID", "AQ_SESSION_ID", "AQ_CLAIM_EPOCH"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("fmt", ["json", "yaml", "stdin"])
def test_batch_propose_loads_full_document(tmp_path, fmt):
    import yaml

    document = {
        "source": "spec:changes",
        "tasks": [{"tempId": "new", "title": "New", "description": "Line 1\nLine 2"}],
        "edits": [{"task_id": "existing", "action": "pause"}],
        "edges": [{"from": "existing", "to": "new"}],
        "remove_edges": [{"from": "existing", "to": "old", "dep_type": "blocks"}],
        "comments": [{"task_id": "new", "body": "Evidence\nSecond line"}],
    }
    content = yaml.safe_dump(document) if fmt == "yaml" else json.dumps(document)
    path = tmp_path / f"changes.{fmt}"
    path.write_text(content)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.execute.return_value = {"success": True, "dry_run": True, "diff": document}
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "--json",
                "task",
                "batch-propose",
                "--project",
                "p",
                "--file",
                "-" if fmt == "stdin" else str(path),
                "--dry-run",
            ],
            input=content,
        )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_batch_propose",
        {
            **document,
            "project_id": "p",
            "dry_run": True,
        },
    )
    assert json.loads(result.output)["data"]["diff"] == document


@pytest.mark.parametrize("document", ["[]", '{"editz": []}', "malformed: ["])
def test_invalid_file_refuses_before_calling_daemon(document):
    client = AsyncMock()
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["task", "batch-propose", "--file", "-"], input=document)
    assert result.exit_code != 0
    client.execute.assert_not_called()


def test_file_source_and_project_can_be_overridden():
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.execute.return_value = {"success": True, "proposal_id": "prop-new"}
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "--json",
                "task",
                "batch-propose",
                "--file",
                "-",
                "--project",
                "p",
                "--source",
                "spec:new",
            ],
            input=json.dumps(
                {
                    "project_id": "old",
                    "source": "spec:old",
                    "edits": [{"task_id": "t", "title": "Changed"}],
                }
            ),
        )
    assert result.exit_code == 0, result.output
    args = client.execute.await_args.args[1]
    assert args["project_id"] == "p" and args["source"] == "spec:new"
    assert args["dry_run"] is False


def test_inline_batch_options_keep_existing_cli_compatible():
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.execute.return_value = {"success": True, "proposal_id": "prop-new"}
    specs = [{"tempId": "new", "title": "New", "description": ""}]
    with patch("src.cli.tasks._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "task",
                "batch-propose",
                "--project-id",
                "p",
                "--source",
                "spec:old-cli",
                "--tasks",
                json.dumps(specs),
                "--edges",
                "[]",
            ],
        )
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with(
        "task_batch_propose",
        {
            "source": "spec:old-cli",
            "project_id": "p",
            "tasks": specs,
            "edges": [],
            "dry_run": False,
        },
    )
