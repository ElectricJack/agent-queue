"""Interactive selection, preview writes and project-create policy parity."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import json
import pytest
from click.testing import CliRunner


def client_for(calls):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)

    async def execute(command, args):
        calls.append((command, args.copy()))
        if command == "create_project":
            return {"success": True, "created": "new-project"}
        if command == "policy_diff":
            selection = args.get("selections", {}).get("template:project:spec.base", {})
            return {
                "success": True,
                "name": "Demo",
                "placeholders": [],
                "values": {},
                "items": [
                    {
                        "id": "template:project:spec.base",
                        "name": "spec.base",
                        "type": "template",
                        "original_scope": "project",
                        "scope": selection.get("scope", "project"),
                        "status": "will overwrite",
                        "state": "copy",
                        "requires_scope_choice": False,
                        "selected": selection.get("overwrite", False),
                        "current_checksum": "sha256:existing",
                        "diff": "-old\n+new\n",
                    }
                ],
            }
        if command == "policy_apply":
            return {
                "success": True,
                "applied": ["template:project:spec.base"],
                "reviews": [],
                "pending_configuration": [],
            }
        if command == "policy_export":
            return {
                "success": True,
                "checksum": "sha256:preview",
                "files": [{"path": "manifest.json", "content": "exact preview"}],
                "written": ["manifest.json"] if "expected_checksum" in args else [],
            }
        raise AssertionError(command)

    client.execute = AsyncMock(side_effect=execute)
    return client


def test_project_create_from_policy_opens_shared_selection_and_overwrites_only_on_optin(tmp_path):
    from src.cli.app import cli

    path = tmp_path / "example.aqpolicy"
    path.touch()
    calls = []
    client = client_for(calls)
    with (
        patch("src.cli.policy._get_client", return_value=client),
        patch("src.cli.app._get_client", return_value=client),
    ):
        result = CliRunner().invoke(
            cli,
            ["project", "create", "--name", "New project", "--from-policy", str(path)],
            input="project\ny\ny\n",
        )
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "create_project" and calls[0][1]["name"] == "New project"
    apply = next(args for command, args in calls if command == "policy_apply")
    assert apply["project_id"] == "new-project"
    assert apply["selections"]["template:project:spec.base"] == {
        "scope": "project",
        "overwrite": True,
        "expected_checksum": "sha256:existing",
    }
    assert "will overwrite" in result.output and "-old" in result.output


def test_apply_yes_and_no_overwrite_keep_existing_items_unselected(tmp_path):
    from src.cli.app import cli

    path = tmp_path / "example.aqpolicy"
    path.touch()
    calls = []
    with patch("src.cli.policy._get_client", return_value=client_for(calls)):
        result = CliRunner().invoke(
            cli,
            [
                "policy",
                "apply",
                str(path),
                "--project",
                "p",
                "--scope",
                "template:project:spec.base=global",
                "--only",
                "template:project:spec.base",
                "--no-overwrite",
                "--yes",
            ],
        )
    assert result.exit_code == 0, result.output
    args = calls[-1][1]
    assert args["no_overwrite"] and args["only"] == ["template:project:spec.base"]
    assert args["selections"]["template:project:spec.base"]["scope"] == "global"
    assert not args["selections"]["template:project:spec.base"]["overwrite"]


def test_export_shows_exact_files_before_acknowledged_write(tmp_path):
    from src.cli.app import cli

    calls = []
    with patch("src.cli.policy._get_client", return_value=client_for(calls)):
        result = CliRunner().invoke(
            cli,
            ["policy", "export", "--project", "p", "--out", str(tmp_path / "example")],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert result.output.index("exact preview") < result.output.index("Write these files")
    assert (
        "expected_checksum" not in calls[0][1]
        and calls[1][1]["expected_checksum"] == "sha256:preview"
    )


@pytest.mark.parametrize("command", ["export", "apply", "create"])
def test_interactive_json_mode_rejected_before_any_mutation(tmp_path, command):
    from src.cli.app import cli

    path = tmp_path / "example.aqpolicy"
    path.touch()
    args = {
        "export": ["policy", "export", "--project", "p", "--out", str(path)],
        "apply": ["policy", "apply", str(path), "--project", "p"],
        "create": ["project", "create", "--name", "New", "--from-policy", str(path)],
    }[command]
    calls = []
    with patch("src.cli.policy._get_client", return_value=client_for(calls)):
        result = CliRunner().invoke(cli, ["--json", *args])
    assert result.exit_code == 2 and not calls
    assert json.loads(result.output)["error"]["code"] == "usage_error"


def test_apply_yes_requires_explicit_system_scope(tmp_path):
    from src.cli.app import cli

    path = tmp_path / "example.aqpolicy"
    path.touch()
    calls = []
    client = client_for(calls)
    original = client.execute.side_effect

    async def execute(command, args):
        data = await original(command, args)
        if command == "policy_diff":
            data["items"][0].update(original_scope="system", requires_scope_choice=True)
        return data

    client.execute.side_effect = execute
    with patch("src.cli.policy._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["policy", "apply", str(path), "--project", "p", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls[-1][1]["selections"]["template:project:spec.base"]["scope"] == "skip"
