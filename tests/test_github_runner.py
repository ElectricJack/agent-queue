from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.git.github_app import AppTokenCandidate
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubRepositoryBinding,
)
from src.git.github_cli import ExistingLoginCredentials, GhRunner


REPOSITORY = GitHubRepositoryBinding(303, "acme/widgets")


@dataclass
class FakeAppCredentials:
    tokens: dict[str, str]
    calls: list[GitHubRepositoryBinding] = field(default_factory=list)

    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        return GitHubCredentialIdentity.app(101, 202)

    async def token_for(self, repository: GitHubRepositoryBinding) -> str:
        self.calls.append(repository)
        return self.tokens[repository.full_name]


class FailingAppCredentials:
    @property
    def credential_identity(self) -> GitHubCredentialIdentity:
        return GitHubCredentialIdentity.app(101, 202)

    async def token_for(self, repository: GitHubRepositoryBinding) -> str:
        del repository
        raise GitHubAccessError("credentials", "installation token unavailable")


def _write_executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/python3\n" + source)
    path.chmod(0o700)
    return path


def _capture_executable(tmp_path: Path) -> Path:
    return _write_executable(
        tmp_path / "gh",
        "import json, os, sys\n"
        "payload = {\n"
        "    'argv': sys.argv[1:],\n"
        "    'stdin': sys.stdin.read(),\n"
        "    'cwd': os.getcwd(),\n"
        "    'env': dict(os.environ),\n"
        "}\n"
        "sys.stdout.write(json.dumps(payload))\n",
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_file(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


async def _assert_pids_gone(*pids: int) -> None:
    for _ in range(200):
        if not any(_pid_exists(pid) for pid in pids):
            return
        await asyncio.sleep(0.01)
    assert not [pid for pid in pids if _pid_exists(pid)]


@pytest.mark.asyncio
async def test_existing_login_preserves_auth_discovery_but_scrubs_target_overrides(tmp_path):
    executable = _capture_executable(tmp_path)
    controlled_cwd = tmp_path / "runner-cwd"
    controlled_cwd.mkdir()
    base_env = {
        "HOME": "/operator/home",
        "GH_CONFIG_DIR": "/operator/gh-config",
        "GH_TOKEN": "operator-gh-token",
        "GITHUB_TOKEN": "operator-github-token",
        "GH_REPO": "attacker/other",
        "GH_HOST": "attacker.example",
        "GH_DEBUG": "api",
        "GH_HTTP_UNIX_SOCKET": "/tmp/attacker.sock",
        "GIT_DIR": "/attacker/repository.git",
        "GIT_WORK_TREE": "/attacker/worktree",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.https://attacker.example/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
    }
    original_env = dict(base_env)
    runner = GhRunner(
        ExistingLoginCredentials(),
        executable=str(executable),
        env=base_env,
        cwd=controlled_cwd,
    )

    result = await runner.run(
        ["api", "--method", "POST", "repos/acme/widgets/check-runs", "--input", "-"],
        repository=REPOSITORY,
        stdin='{"name":"required"}',
    )

    capture = json.loads(result.stdout)
    assert capture["argv"] == [
        "api",
        "--hostname",
        "github.com",
        "--method",
        "POST",
        "repos/acme/widgets/check-runs",
        "--input",
        "-",
    ]
    assert capture["stdin"] == '{"name":"required"}'
    assert capture["cwd"] == str(controlled_cwd)
    assert capture["env"]["HOME"] == "/operator/home"
    assert capture["env"]["GH_CONFIG_DIR"] == "/operator/gh-config"
    assert capture["env"]["GH_TOKEN"] == "operator-gh-token"
    assert capture["env"]["GITHUB_TOKEN"] == "operator-github-token"
    for removed in (
        "GH_REPO",
        "GH_HOST",
        "GH_DEBUG",
        "GH_HTTP_UNIX_SOCKET",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    ):
        assert removed not in capture["env"]
    assert capture["env"]["GH_PROMPT_DISABLED"] == "1"
    assert capture["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert capture["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert capture["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert base_env == original_env


@pytest.mark.asyncio
async def test_repository_commands_receive_runner_owned_repo_target(tmp_path):
    executable = _capture_executable(tmp_path)
    runner = GhRunner(ExistingLoginCredentials(), executable=str(executable), env={}, cwd=tmp_path)

    result = await runner.run(["pr", "view", "7", "--json", "headRefOid"], repository=REPOSITORY)

    capture = json.loads(result.stdout)
    assert capture["argv"] == [
        "pr",
        "view",
        "7",
        "--json",
        "headRefOid",
        "--repo",
        "acme/widgets",
    ]
    assert "--slurp" not in capture["argv"]
    assert "baseRefOid" not in capture["argv"]


@pytest.mark.asyncio
async def test_pr_body_is_passed_on_stdin_and_never_in_argv(tmp_path):
    executable = _capture_executable(tmp_path)
    runner = GhRunner(ExistingLoginCredentials(), executable=str(executable), env={}, cwd=tmp_path)
    body = "reviewed body sentinel"

    result = await runner.run(
        [
            "pr",
            "create",
            "--title",
            "Delivery",
            "--base",
            "main",
            "--head",
            "aq/topic",
            "--body-file",
            "-",
        ],
        repository=REPOSITORY,
        stdin=body,
    )

    capture = json.loads(result.stdout)
    assert capture["stdin"] == body
    assert body not in capture["argv"]
    assert capture["argv"][-2:] == ["--repo", "acme/widgets"]


@pytest.mark.asyncio
async def test_concurrent_app_calls_use_distinct_config_and_selected_tokens(tmp_path):
    executable = _capture_executable(tmp_path)
    other = GitHubRepositoryBinding(404, "acme/gadgets")
    tokens = {
        REPOSITORY.full_name: "ghs_widgets_installation_secret",
        other.full_name: "ghs_gadgets_installation_secret",
    }
    credentials = FakeAppCredentials(tokens)
    base_env = {
        "GH_TOKEN": "ambient-personal-token",
        "GITHUB_TOKEN": "ambient-second-token",
        "GH_ENTERPRISE_TOKEN": "ambient-enterprise-token",
        "GH_CONFIG_DIR": "/operator/gh-config",
        "GH_HOST": "attacker.example",
        "GH_REPO": "attacker/other",
        "GH_DEBUG": "api",
    }
    original_env = dict(base_env)
    parent_environment = {
        key: os.environ.get(key)
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_HOST", "GH_REPO")
    }
    runner = GhRunner(credentials, executable=str(executable), env=base_env, cwd=tmp_path)

    first, second = await asyncio.gather(
        runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY),
        runner.run(["api", "repos/acme/gadgets"], repository=other),
    )

    captures = [json.loads(first.stdout), json.loads(second.stdout)]
    by_endpoint = {capture["argv"][-1]: capture for capture in captures}
    widgets = by_endpoint["repos/acme/widgets"]
    gadgets = by_endpoint["repos/acme/gadgets"]
    assert widgets["env"]["GH_TOKEN"] == tokens[REPOSITORY.full_name]
    assert gadgets["env"]["GH_TOKEN"] == tokens[other.full_name]
    for capture in captures:
        assert capture["env"]["GH_HOST"] == "github.com"
        assert capture["env"]["GH_CONFIG_DIR"] != "/operator/gh-config"
        assert "GITHUB_TOKEN" not in capture["env"]
        assert "GH_ENTERPRISE_TOKEN" not in capture["env"]
        assert "GH_REPO" not in capture["env"]
        assert "GH_DEBUG" not in capture["env"]
        assert not any(secret in " ".join(capture["argv"]) for secret in tokens.values())
    assert widgets["env"]["GH_CONFIG_DIR"] != gadgets["env"]["GH_CONFIG_DIR"]
    assert set(credentials.calls) == {REPOSITORY, other}
    assert base_env == original_env
    assert {
        key: os.environ.get(key)
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_HOST", "GH_REPO")
    } == parent_environment


@pytest.mark.asyncio
async def test_explicit_candidate_bypasses_provider_and_unused_personal_tokens(tmp_path):
    executable = _capture_executable(tmp_path)
    credentials = FakeAppCredentials({REPOSITORY.full_name: "must-not-be-resolved"})
    runner = GhRunner(
        credentials,
        executable=str(executable),
        env={
            "GH_TOKEN": "available-personal-token",
            "GITHUB_TOKEN": "available-secondary-token",
            "GH_CONFIG_DIR": "/operator/gh-config",
        },
        cwd=tmp_path,
    )
    candidate = AppTokenCandidate(
        identity=credentials.credential_identity,
        repository=REPOSITORY,
        token="ghs_new_candidate_secret",
        expires_at=1_900_000_000.0,
    )

    result = await runner.run(
        ["api", "repos/acme/widgets"],
        repository=REPOSITORY,
        credential=candidate,
    )

    capture = json.loads(result.stdout)
    assert credentials.calls == []
    assert capture["env"]["GH_TOKEN"] == "ghs_new_candidate_secret"
    assert "GITHUB_TOKEN" not in capture["env"]
    assert capture["env"]["GH_CONFIG_DIR"] != "/operator/gh-config"


@pytest.mark.asyncio
async def test_explicit_candidate_is_fenced_to_runner_identity_and_repository(tmp_path):
    executable = _capture_executable(tmp_path)
    credentials = FakeAppCredentials({REPOSITORY.full_name: "unused"})
    runner = GhRunner(credentials, executable=str(executable), env={}, cwd=tmp_path)
    foreign_repository = GitHubRepositoryBinding(404, "acme/gadgets")
    foreign_identity = AppTokenCandidate(
        identity=GitHubCredentialIdentity.app(999, 202),
        repository=REPOSITORY,
        token="foreign-identity",
        expires_at=1_900_000_000.0,
    )
    foreign_binding = AppTokenCandidate(
        identity=credentials.credential_identity,
        repository=foreign_repository,
        token="foreign-repository",
        expires_at=1_900_000_000.0,
    )

    with pytest.raises(ValueError, match="identity"):
        await runner.run(
            ["api", "repos/acme/widgets"],
            repository=REPOSITORY,
            credential=foreign_identity,
        )
    with pytest.raises(ValueError, match="repository"):
        await runner.run(
            ["api", "repos/acme/widgets"],
            repository=REPOSITORY,
            credential=foreign_binding,
        )

    assert credentials.calls == []


@pytest.mark.asyncio
async def test_app_credential_failure_prevents_process_launch(tmp_path):
    marker = tmp_path / "launched"
    executable = _write_executable(
        tmp_path / "gh",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('launched')\n",
    )
    runner = GhRunner(FailingAppCredentials(), executable=str(executable), env={}, cwd=tmp_path)

    with pytest.raises(GitHubAccessError, match="installation token unavailable"):
        await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_missing_executable_is_safe_structured_error(tmp_path):
    runner = GhRunner(
        ExistingLoginCredentials(),
        executable=str(tmp_path / "missing-gh"),
        env={},
        cwd=tmp_path,
    )

    with pytest.raises(GitHubAccessError) as excinfo:
        await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY)

    assert excinfo.value.category == "cli_missing"
    assert str(tmp_path) not in str(excinfo.value)


@pytest.mark.asyncio
async def test_executable_is_resolved_from_captured_path(tmp_path, monkeypatch):
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    executable = _capture_executable(executable_dir)
    base_env = {"PATH": str(executable_dir)}
    runner = GhRunner(ExistingLoginCredentials(), executable="gh", env=base_env, cwd=tmp_path)
    base_env["PATH"] = "/attacker/bin"
    monkeypatch.setenv("PATH", "/attacker/bin")

    result = await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY)

    assert json.loads(result.stdout)["argv"][-1] == "repos/acme/widgets"
    assert runner._resolved_executable == str(executable.resolve())


@pytest.mark.asyncio
async def test_missing_executable_is_reported_before_app_token_resolution(tmp_path):
    credentials = FakeAppCredentials({REPOSITORY.full_name: "ghs_unused_secret"})
    runner = GhRunner(
        credentials,
        executable=str(tmp_path / "missing-gh"),
        env={},
        cwd=tmp_path,
    )

    with pytest.raises(GitHubAccessError) as excinfo:
        await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY)

    assert excinfo.value.category == "cli_missing"
    assert credentials.calls == []


@pytest.mark.asyncio
async def test_output_limit_is_enforced_while_process_is_running(tmp_path):
    pid_file = tmp_path / "leader.pid"
    executable = _write_executable(
        tmp_path / "gh",
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdout.buffer.write(b'x' * 65536)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(300)\n",
    )
    runner = GhRunner(
        ExistingLoginCredentials(),
        executable=str(executable),
        env={},
        cwd=tmp_path,
        timeout=5,
        max_stdout_bytes=1024,
        cleanup_timeout=2,
    )

    with pytest.raises(GitHubAccessError, match="output exceeded size limit"):
        await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY)

    await _wait_for_file(pid_file)
    await _assert_pids_gone(int(pid_file.read_text()))


def _descendant_executable(tmp_path: Path) -> tuple[Path, Path]:
    pid_file = tmp_path / "processes.json"
    executable = _write_executable(
        tmp_path / "gh",
        "import json, os, subprocess, time\n"
        "child = subprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(300)'])\n"
        f"open({str(pid_file)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(300)\n",
    )
    return executable, pid_file


@pytest.mark.asyncio
async def test_timeout_kills_leader_and_descendants(tmp_path):
    executable, pid_file = _descendant_executable(tmp_path)
    runner = GhRunner(
        ExistingLoginCredentials(),
        executable=str(executable),
        env={},
        cwd=tmp_path,
        timeout=0.2,
        cleanup_timeout=2,
    )

    with pytest.raises(GitHubAccessError, match="timed out"):
        await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY)

    await _wait_for_file(pid_file)
    await _assert_pids_gone(*json.loads(pid_file.read_text()))


@pytest.mark.asyncio
async def test_cancellation_kills_leader_and_descendants(tmp_path):
    executable, pid_file = _descendant_executable(tmp_path)
    runner = GhRunner(
        ExistingLoginCredentials(),
        executable=str(executable),
        env={},
        cwd=tmp_path,
        timeout=30,
        cleanup_timeout=2,
    )
    task = asyncio.create_task(runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY))
    await _wait_for_file(pid_file)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _assert_pids_gone(*json.loads(pid_file.read_text()))


@pytest.mark.asyncio
async def test_machine_output_is_intact_and_diagnostics_are_scrubbed(tmp_path):
    secret = "ghs_installation_secret_value"
    executable = _write_executable(
        tmp_path / "gh",
        "import os, sys\n"
        "secret = os.environ['GH_TOKEN']\n"
        "sys.stdout.buffer.write(b'\\x00machine-response\\n')\n"
        "sys.stderr.write(f'Authorization: Bearer {secret} "
        "GH_TOKEN={secret} github_pat_another_secret harmless')\n"
        "raise SystemExit(7)\n",
    )
    runner = GhRunner(
        FakeAppCredentials({REPOSITORY.full_name: secret}),
        executable=str(executable),
        env={},
        cwd=tmp_path,
    )

    result = await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY, check=False)

    assert result.returncode == 7
    assert result.stdout == b"\x00machine-response\n"
    assert secret not in result.stderr
    assert "github_pat_another_secret" not in result.stderr
    assert result.stderr.count("[REDACTED]") == 3
    assert "harmless" in result.stderr


@pytest.mark.asyncio
async def test_existing_login_ambient_token_is_scrubbed_from_diagnostics(tmp_path):
    secret = "opaque-operator-token"
    executable = _write_executable(
        tmp_path / "gh",
        "import os, sys\n"
        "sys.stderr.write('diagnostic ' + os.environ['GH_TOKEN'])\n"
        "raise SystemExit(2)\n",
    )
    runner = GhRunner(
        ExistingLoginCredentials(),
        executable=str(executable),
        env={"GH_TOKEN": secret},
        cwd=tmp_path,
    )

    result = await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY, check=False)

    assert result.returncode == 2
    assert secret not in result.stderr
    assert result.stderr == "diagnostic [REDACTED]"


@pytest.mark.asyncio
async def test_runner_rejects_caller_target_override_and_oversized_stdin(tmp_path):
    executable = _capture_executable(tmp_path)
    runner = GhRunner(
        ExistingLoginCredentials(),
        executable=str(executable),
        env={},
        cwd=tmp_path,
        max_stdin_bytes=4,
    )

    with pytest.raises(ValueError, match="runner-controlled"):
        await runner.run(
            ["api", "--hostname=attacker.example", "repos/acme/widgets"],
            repository=REPOSITORY,
        )
    with pytest.raises(ValueError, match="stdin exceeded"):
        await runner.run(["api", "repos/acme/widgets"], repository=REPOSITORY, stdin=b"12345")


@pytest.mark.asyncio
async def test_runner_rejects_body_flags_and_known_credentials_in_argv(tmp_path):
    executable = _capture_executable(tmp_path)
    secret = "ghp_personal_secret"
    runner = GhRunner(
        ExistingLoginCredentials(),
        executable=str(executable),
        env={"GH_TOKEN": secret},
        cwd=tmp_path,
    )

    with pytest.raises(ValueError, match="API bodies"):
        await runner.run(
            ["api", "repos/acme/widgets", "--raw-field", "name=value"],
            repository=REPOSITORY,
        )
    with pytest.raises(ValueError, match="PR bodies"):
        await runner.run(["pr", "create", "--body", "body in argv"], repository=REPOSITORY)
    with pytest.raises(ValueError, match="credentials"):
        await runner.run(["api", f"repos/acme/widgets?token={secret}"], repository=REPOSITORY)
