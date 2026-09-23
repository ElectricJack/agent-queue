"""Repository provisioning uses the startup GitHub runner and capability boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import GitHubAppConfig
from src.git.github import GitHubAccess
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitManager


def _fake_gh(tmp_path: Path) -> Path:
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with Path(os.environ['AQ_GH_LOG']).open('a') as log:\n"
        "    log.write(json.dumps({'args': args, 'host': os.environ.get('GH_HOST'), "
        "'token': os.environ.get('GH_TOKEN'), "
        "'github_token': os.environ.get('GITHUB_TOKEN'), "
        "'config': os.environ.get('GH_CONFIG_DIR'), "
        "'prompt': os.environ.get('GH_PROMPT_DISABLED')}) + '\\n')\n"
        "if args[:2] == ['auth', 'status']:\n"
        "    sys.exit(4 if os.environ.get('AQ_GH_AUTH_FAIL') else 0)\n"
        "if args[:2] == ['repo', 'create']:\n"
        "    if os.environ.get('AQ_GH_CREATE_FAIL'):\n"
        "        sys.stderr.write('GraphQL: Name already exists\\n')\n"
        "        sys.exit(1)\n"
        "    full_name = args[2] if '/' in args[2] else 'personal/' + args[2]\n"
        "    print('https://github.com/' + full_name)\n"
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize(
    "login_env",
    [
        {"GH_TOKEN": "pat-value"},
        {"GITHUB_TOKEN": "pat-value"},
        {"GH_CONFIG_DIR": "/stored/gh"},
    ],
)
@pytest.mark.parametrize("org", [None, "my-org"])
async def test_existing_login_creates_repository_through_shared_runner(tmp_path, login_env, org):
    log = tmp_path / "gh.jsonl"
    access = GitHubAccess.from_config(
        executable=str(_fake_gh(tmp_path)),
        cwd=tmp_path,
        env={**login_env, "AQ_GH_LOG": str(log), "GH_HOST": "wrong.example"},
    )

    url = await GitManager(access).acreate_github_repo(
        "my-app", private=False, org=org, description="A cool app"
    )

    assert url == f"https://github.com/{org or 'personal'}/my-app"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["args"] for call in calls] == [
        ["auth", "status", "--hostname", "github.com"],
        [
            "repo",
            "create",
            f"{org + '/' if org else ''}my-app",
            "--public",
            "--description",
            "A cool app",
        ],
    ]
    assert all(call["host"] == "github.com" and call["prompt"] == "1" for call in calls)
    assert all(call["token"] == login_env.get("GH_TOKEN") for call in calls)
    assert all(call["github_token"] == login_env.get("GITHUB_TOKEN") for call in calls)
    assert all(call["config"] == login_env.get("GH_CONFIG_DIR") for call in calls)


async def test_existing_login_auth_failure_prevents_creation(tmp_path):
    log = tmp_path / "gh.jsonl"
    access = GitHubAccess.from_config(
        executable=str(_fake_gh(tmp_path)),
        cwd=tmp_path,
        env={"AQ_GH_LOG": str(log), "AQ_GH_AUTH_FAIL": "1"},
    )

    with pytest.raises(GitHubAccessError) as failure:
        await access.create_repository("my-app")

    assert failure.value.category == "credentials"
    assert [json.loads(line)["args"][:2] for line in log.read_text().splitlines()] == [
        ["auth", "status"]
    ]


async def test_app_mode_rejects_creation_before_using_personal_login(tmp_path):
    log = tmp_path / "gh.jsonl"
    access = GitHubAccess.from_config(
        GitHubAppConfig("Iv1.client", 101, 202, "/daemon/key.pem"),
        executable=str(_fake_gh(tmp_path)),
        cwd=tmp_path,
        env={"AQ_GH_LOG": str(log), "GH_TOKEN": "personal-pat"},
    )

    with pytest.raises(GitHubAccessError) as failure:
        await GitManager(access).acreate_github_repo("my-app")

    assert failure.value.category == "github_operation_unsupported"
    assert "personal-pat" not in str(failure.value)
    assert not log.exists()


async def test_existing_login_reports_repository_conflict(tmp_path):
    log = tmp_path / "gh.jsonl"
    access = GitHubAccess.from_config(
        executable=str(_fake_gh(tmp_path)),
        cwd=tmp_path,
        env={"AQ_GH_LOG": str(log), "AQ_GH_CREATE_FAIL": "1"},
    )

    with pytest.raises(GitHubAccessError) as failure:
        await access.create_repository("my-app")

    assert failure.value.category == "conflict_or_invalid"
    assert "already exists" in str(failure.value)
    assert len(log.read_text().splitlines()) == 2


async def test_invalid_repository_name_is_rejected_before_launch(tmp_path):
    log = tmp_path / "gh.jsonl"
    access = GitHubAccess.from_config(
        executable=str(_fake_gh(tmp_path)), cwd=tmp_path, env={"AQ_GH_LOG": str(log)}
    )

    with pytest.raises(GitHubAccessError) as failure:
        await access.create_repository("--dangerous")

    assert failure.value.category == "conflict_or_invalid"
    assert not log.exists()
