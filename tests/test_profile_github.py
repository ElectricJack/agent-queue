"""Profile gist sharing uses the startup-owned GitHub capability boundary."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.profile_commands import ProfileCommandsMixin
from src.config import GitHubAppConfig
from src.git.github import GitHubAccess
from src.git.github_contracts import GitHubAccessError


GIST_ID = "a" * 32
GIST_URL = f"https://gist.github.com/octocat/{GIST_ID}"
TOKEN = "ghp_" + "secret" * 5


class _ProfileCommands(ProfileCommandsMixin):
    def __init__(self, access: GitHubAccess, data_dir: Path) -> None:
        self.orchestrator = SimpleNamespace(github_access=access)
        self.config = SimpleNamespace(data_dir=str(data_dir))
        self.db = SimpleNamespace(get_profile=self._get_profile)

    @staticmethod
    async def _get_profile(profile_id: str):
        if profile_id != "demo":
            return None
        return SimpleNamespace(
            id="demo",
            name="Demo",
            description="",
            default_class="",
            harness=None,
            permission_mode="",
            codex_full_auto=False,
            claude_dangerously_skip_permissions=False,
            allowed_tools=[],
            mcp_servers={},
            system_prompt_suffix="",
            install={},
        )


def _fake_gh(tmp_path: Path) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "mode = os.environ.get('AQ_FAKE_GH_MODE', 'ok')\n"
        "if mode == 'hang':\n"
        "    Path(os.environ['AQ_FAKE_GH_MARKER']).write_text(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "if mode == 'fail':\n"
        "    sys.stderr.write('HTTP 401 GH_TOKEN=' + os.environ.get('GH_TOKEN', '') + '\\n')\n"
        "    sys.exit(4)\n"
        "state = Path(os.environ['AQ_FAKE_GH_STATE'])\n"
        "args = sys.argv[1:]\n"
        "if 'gists' in args:\n"
        "    body = json.loads(sys.stdin.read())\n"
        "    state.write_text(json.dumps({'args': args, 'body': body, "
        "'prompt': os.environ.get('GH_PROMPT_DISABLED'), "
        "'gh_repo': os.environ.get('GH_REPO')}))\n"
        "    result = {'id': '" + GIST_ID + "', "
        "'html_url': '" + GIST_URL + "'}\n"
        "    status = '201 Created'\n"
        "else:\n"
        "    saved = json.loads(state.read_text())\n"
        "    content = next(iter(saved['body']['files'].values()))['content']\n"
        "    result = {'id': '" + GIST_ID + "', "
        "'files': {'profile.yaml': {'content': content, 'truncated': False}}}\n"
        "    status = '200 OK'\n"
        "sys.stdout.write('HTTP/2 ' + status + '\\r\\nContent-Type: application/json' "
        "+ '\\r\\n\\r\\n' + json.dumps(result))\n"
    )
    executable.chmod(0o700)
    return executable


def _access(tmp_path: Path, *, mode: str = "ok", timeout: float = 1.0) -> GitHubAccess:
    return GitHubAccess.from_config(
        executable=str(_fake_gh(tmp_path)),
        cwd=tmp_path,
        timeout=timeout,
        env={
            "GH_TOKEN": TOKEN,
            "GH_REPO": "attacker/other",
            "AQ_FAKE_GH_MODE": mode,
            "AQ_FAKE_GH_STATE": str(tmp_path / "gist.json"),
            "AQ_FAKE_GH_MARKER": str(tmp_path / "started"),
        },
    )


@pytest.mark.asyncio
async def test_profile_gist_roundtrip_uses_shared_runner(tmp_path, monkeypatch):
    from src.profiles import sync

    monkeypatch.setattr(
        sync,
        "sync_profile_text_to_db",
        AsyncMock(return_value=SimpleNamespace(warnings=[])),
    )
    commands = _ProfileCommands(_access(tmp_path), tmp_path / "data")

    exported = await commands._cmd_export_profile({"profile_id": "demo", "create_gist": True})
    assert exported["gist_url"] == GIST_URL
    saved = json.loads((tmp_path / "gist.json").read_text())
    assert saved["args"][:3] == ["api", "--hostname", "github.com"]
    assert saved["args"][-2:] == ["--input", "-"]
    assert saved["body"]["public"] is True
    assert saved["body"]["files"]["agent-profile-demo.yaml"]["content"] == exported["yaml"]
    assert saved["prompt"] == "1"
    assert saved["gh_repo"] is None

    imported = await commands._cmd_import_profile({"source": GIST_URL, "id": "copy"})
    assert imported["imported"] is True
    assert imported["id"] == "copy"
    assert (tmp_path / "data" / "vault" / "agent-types" / "copy" / "profile.md").exists()


@pytest.mark.asyncio
async def test_app_mode_rejects_profile_gists_before_personal_login_or_launch(tmp_path):
    executable = _fake_gh(tmp_path)
    access = GitHubAccess.from_config(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        executable=str(executable),
        cwd=tmp_path,
        env={"GH_TOKEN": TOKEN, "AQ_FAKE_GH_MARKER": str(tmp_path / "started")},
    )
    commands = _ProfileCommands(access, tmp_path / "data")

    exported = await commands._cmd_export_profile({"profile_id": "demo", "create_gist": True})
    imported = await commands._cmd_import_profile({"source": GIST_URL})

    assert "yaml" in exported
    assert "gist_url" not in exported
    assert exported["gist_error_code"] == "github_operation_unsupported"
    assert imported["error_code"] == "github_operation_unsupported"
    assert not (tmp_path / "started").exists()
    assert not (tmp_path / "gist.json").exists()
    assert TOKEN not in str(exported) + str(imported)


@pytest.mark.asyncio
async def test_profile_gist_errors_are_bounded_and_redacted(tmp_path):
    commands = _ProfileCommands(_access(tmp_path, mode="fail"), tmp_path / "data")
    exported = await commands._cmd_export_profile({"profile_id": "demo", "create_gist": True})
    imported = await commands._cmd_import_profile({"source": GIST_URL})
    assert exported["gist_error_code"] == "credentials"
    assert imported["error_code"] == "credentials"
    assert TOKEN not in str(exported) + str(imported)

    timed_out = _ProfileCommands(_access(tmp_path, mode="hang", timeout=0.05), tmp_path / "data")
    result = await timed_out._cmd_export_profile({"profile_id": "demo", "create_gist": True})
    assert result["gist_error_code"] == "transient"
    assert "timed out" in result["gist_error"]


@pytest.mark.asyncio
async def test_profile_gist_read_cancellation_reaps_process(tmp_path):
    access = _access(tmp_path, mode="hang")
    task = asyncio.create_task(access.read_profile_gist(GIST_URL))
    marker = tmp_path / "started"
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    pid = int(marker.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("cancelled gh process survived")


@pytest.mark.asyncio
async def test_profile_gist_rejects_foreign_url_before_launch(tmp_path):
    access = _access(tmp_path)
    with pytest.raises(GitHubAccessError) as error:
        await access.read_profile_gist("https://example.com/octocat/" + GIST_ID)
    assert error.value.category == "conflict_or_invalid"
    with pytest.raises(GitHubAccessError) as malformed:
        await access.read_profile_gist("https://[invalid/" + GIST_ID)
    assert malformed.value.category == "conflict_or_invalid"
    assert not (tmp_path / "gist.json").exists()
