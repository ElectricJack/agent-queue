"""Interactive selection, preview writes and project-create policy parity."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import json
import pytest
from click.testing import CliRunner


def creation_args(command):
    if command == "create":
        return ["project", "create", "--name", "New"]
    return [
        "project", "onboard", "--root-id", "dev", "--relative-path", "new-project",
        "--project-name", "New", "--project-id", "new-project",
    ]


def client_for(calls):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)

    async def execute(command, args):
        calls.append((command, args.copy()))
        if command == "create_project":
            return {"success": True, "created": "new-project"}
        if command == "onboard_project":
            return {"success": True, "project_id": "new-project"}
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


def test_export_displays_source_warnings_before_acknowledged_write(tmp_path):
    from src.cli.app import cli

    calls = []
    client = client_for(calls)
    original = client.execute.side_effect
    message = "Skipped legacy: source unavailable, recompile and reactivate legacy"

    async def execute(command, args):
        data = await original(command, args)
        data["warnings"] = [{"item_id": "playbook:system:legacy", "playbook_id": "legacy", "message": message}]
        return data

    client.execute.side_effect = execute
    with patch("src.cli.policy._get_client", return_value=client):
        result = CliRunner().invoke(
            cli, ["policy", "export", "--project", "p", "--out", str(tmp_path / "example")],
            input="y\n",
        )
    assert result.exit_code == 0, result.output
    assert result.output.index("source unavailable") < result.output.index("Write these files")
    assert calls[-1][1]["expected_checksum"] == "sha256:preview"


@pytest.mark.parametrize("command", ["export", "apply", "create", "onboard"])
def test_interactive_json_mode_rejected_before_any_mutation(tmp_path, command):
    from src.cli.app import cli

    path = tmp_path / "example.aqpolicy"
    path.touch()
    args = {
        "export": ["policy", "export", "--project", "p", "--out", str(path)],
        "apply": ["policy", "apply", str(path), "--project", "p"],
        "create": ["project", "create", "--name", "New", "--from-policy", str(path)],
        "onboard": [*creation_args("onboard"), "--from-policy", str(path)],
    }[command]
    calls = []
    client = client_for(calls)
    with (
        patch("src.cli.policy._get_client", return_value=client),
        patch("src.cli.app._get_client", return_value=client),
        patch("src.cli.projects._get_client", return_value=client),
    ):
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


@pytest.mark.parametrize("command", ["create", "onboard"])
@pytest.mark.parametrize("profile", [None, "none", "default", "exported"])
def test_project_creation_profile_choice(tmp_path, command, profile):
    from src.cli.app import cli

    calls = []
    client = client_for(calls)
    path = tmp_path / "example.aqpolicy"
    path.touch()
    args = creation_args(command)
    if profile:
        args += ["--from-policy", str(path) if profile == "exported" else profile]
    with (
        patch("src.cli.policy._get_client", return_value=client),
        patch("src.cli.app._get_client", return_value=client),
        patch("src.cli.projects._get_client", return_value=client),
        patch("src.cli.policy.sys.stdin.isatty", return_value=False),
    ):
        result = CliRunner().invoke(cli, args, input="skip\ny\n")
    assert result.exit_code == 0, result.output
    assert calls[0][0] == ("create_project" if command == "create" else "onboard_project")
    assert "Policy profile" not in result.output
    if profile in {"default", "exported"}:
        assert calls[-1][0] == "policy_apply"
        assert calls[-1][1]["project_id"] == "new-project"
        if profile == "default":
            assert calls[-1][1]["builtin"] == "default"
            assert "path" not in calls[-1][1]
        else:
            assert calls[-1][1]["path"] == str(path.resolve())
            assert "builtin" not in calls[-1][1]
    else:
        assert len(calls) == 1


@pytest.mark.parametrize("command", ["create", "onboard"])
@pytest.mark.parametrize("choice", ["default", "none", "exported"])
def test_terminal_creation_asks_for_profile_before_creating(tmp_path, command, choice):
    from src.cli.app import cli

    calls = []
    client = client_for(calls)
    path = tmp_path / "example.aqpolicy"
    path.touch()
    answer = f"{choice}\n" + (f"{path}\n" if choice == "exported" else "") + "skip\ny\n"
    with (
        patch("src.cli.policy.sys") as terminal,
        patch("src.cli.policy._get_client", return_value=client),
        patch("src.cli.app._get_client", return_value=client),
        patch("src.cli.projects._get_client", return_value=client),
    ):
        terminal.stdin.isatty.return_value = True
        result = CliRunner().invoke(
            cli, creation_args(command), input=answer
        )
    assert result.exit_code == 0, result.output
    assert "Policy profile" in result.output
    created_text = "Created project" if command == "create" else "Onboarded"
    assert result.output.index("Policy profile") < result.output.index(created_text)
    assert (calls[-1][0] == "policy_apply") == (choice != "none")


@pytest.mark.parametrize("command", ["create", "onboard"])
@pytest.mark.parametrize("profile", [None, "none"])
def test_json_creation_without_policy_never_prompts_or_calls_policy(command, profile):
    from src.cli.app import cli

    calls = []
    client = client_for(calls)
    with (
        patch("src.cli.app._get_client", return_value=client),
        patch("src.cli.projects._get_client", return_value=client),
        patch("src.cli.policy._get_client", return_value=client),
        patch("src.cli.policy.sys.stdin.isatty", return_value=True),
    ):
        args = creation_args(command) + (["--from-policy", profile] if profile else [])
        result = CliRunner().invoke(cli, ["--json", *args])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["success"]
    assert len(calls) == 1


@pytest.mark.parametrize("command", ["create", "onboard"])
@pytest.mark.parametrize("profile", ["default", "exported"])
def test_cancelled_import_retains_exactly_the_created_project(tmp_path, command, profile):
    from src.cli.app import cli

    calls = []
    client = client_for(calls)
    path = tmp_path / "example.aqpolicy"
    path.touch()
    source = str(path) if profile == "exported" else profile
    with (
        patch("src.cli.app._get_client", return_value=client),
        patch("src.cli.projects._get_client", return_value=client),
        patch("src.cli.policy._get_client", return_value=client),
    ):
        result = CliRunner().invoke(
            cli, [*creation_args(command), "--from-policy", source], input="skip\nn\n",
        )
    assert result.exit_code == 1, result.output
    assert "new-project" in result.output and "Aborted" in result.output
    assert [name for name, _ in calls].count(
        "create_project" if command == "create" else "onboard_project"
    ) == 1
    assert all(name == "policy_diff" for name, _ in calls[1:])
    assert all(args["project_id"] == "new-project" for _, args in calls[1:])


@pytest.mark.parametrize("command", ["create", "onboard"])
@pytest.mark.parametrize("source", ["missing", "json-default"])
def test_invalid_creation_policy_is_rejected_before_creating(tmp_path, command, source):
    from src.cli.app import cli

    calls = []
    client = client_for(calls)
    args = [*creation_args(command), "--from-policy", str(tmp_path / "absent.aqpolicy")]
    if source == "json-default":
        args = ["--json", *creation_args(command), "--from-policy", "default"]
    with (
        patch("src.cli.app._get_client", return_value=client),
        patch("src.cli.projects._get_client", return_value=client),
        patch("src.cli.policy._get_client", return_value=client),
    ):
        result = CliRunner().invoke(cli, args)
    assert result.exit_code == 2, result.output
    assert calls == []
    if source == "missing":
        assert "does not exist" in result.output
    else:
        assert json.loads(result.output)["error"]["code"] == "usage_error"
