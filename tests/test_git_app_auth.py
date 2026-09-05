from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from src.git.askpass_fd import answer_prompt
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitError, GitManager


class FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"", b""


@pytest.mark.asyncio
async def test_app_push_uses_frozen_repository_and_one_shot_fd_without_secret_leak(
    tmp_path, monkeypatch
):
    secret = "token-with-unbounded-length-and-a-sentinel"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    captured = {}

    async def fake_create(*argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        token_fd = kwargs["pass_fds"][0]
        assert os.read(token_fd, 1000).decode() == secret
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token")

    result = await GitManager().apush_oid_with_app_auth(
        str(checkout),
        repository=GitHubRepositoryBinding(303, "acme/widgets"),
        token=secret,
        tip_oid="a" * 40,
        branch="main",
        expected_old_oid="b" * 40,
    )

    assert result == "a" * 40
    assert captured["argv"] == (
        "git",
        "push",
        "https://github.com/acme/widgets.git",
        "--force-with-lease=refs/heads/main:" + "b" * 40,
        "a" * 40 + ":refs/heads/main",
    )
    serialized = repr(captured)
    assert secret not in serialized
    assert "ambient-gh-token" not in serialized
    assert "ambient-github-token" not in serialized
    assert captured["env"]["GIT_ASKPASS"] == str(Path(answer_prompt.__code__.co_filename))
    assert captured["env"]["AQ_GIT_APP_USERNAME"] == "x-access-token"
    assert set(captured["pass_fds"]) == {int(captured["env"]["AQ_GIT_APP_TOKEN_FD"])}


@pytest.mark.asyncio
async def test_app_push_error_never_repeats_remote_output_or_token(tmp_path, monkeypatch):
    secret = "high-authority-secret"

    class FailedProcess(FakeProcess):
        returncode = 1

        async def communicate(self):
            return b"", f"remote echoed {secret}".encode()

    async def fake_create(*argv, **kwargs):
        return FailedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(GitError) as caught:
        await GitManager().apush_oid_with_app_auth(
            str(tmp_path),
            repository=GitHubRepositoryBinding(303, "acme/widgets"),
            token=secret,
            tip_oid="a" * 40,
            branch="main",
            expected_old_oid="b" * 40,
        )
    assert secret not in str(caught.value)
    assert "remote echoed" not in str(caught.value)


def test_askpass_reads_token_only_for_password_prompt():
    with tempfile.TemporaryFile() as token_file:
        token_file.write(b"secret")
        token_file.seek(0)
        token_fd = token_file.fileno()
        assert answer_prompt("Username for github.com", token_fd, "x-access-token") == "x-access-token"
        assert answer_prompt("Password for github.com", token_fd, "x-access-token") == "secret"
        assert answer_prompt("Password again", token_fd, "x-access-token") == ""


@pytest.mark.asyncio
async def test_app_push_timeout_kills_and_reaps_git_before_closing_credential_fd(
    tmp_path, monkeypatch
):
    class HangingProcess:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True
            self.returncode = -9

    process = HangingProcess()

    async def fake_create(*argv, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    manager = GitManager()
    manager._GIT_TIMEOUT = 0.01

    with pytest.raises(GitError, match="authenticated Git push failed"):
        await manager.apush_oid_with_app_auth(
            str(tmp_path),
            repository=GitHubRepositoryBinding(303, "acme/widgets"),
            token="secret",
            tip_oid="a" * 40,
            branch="main",
            expected_old_oid="b" * 40,
        )

    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_askpass_helper_is_directly_executable_with_inherited_fd_only():
    helper = Path(answer_prompt.__code__.co_filename)
    with tempfile.TemporaryFile() as token_file:
        token_file.write(b"helper-secret")
        token_file.seek(0)
        token_fd = token_file.fileno()
        env = {
            "AQ_GIT_APP_TOKEN_FD": str(token_fd),
            "AQ_GIT_APP_USERNAME": "x-access-token",
        }
        process = await asyncio.create_subprocess_exec(
            str(helper),
            "Password for github.com",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            pass_fds=(token_fd,),
        )
        stdout, stderr = await process.communicate()

    assert process.returncode == 0
    assert stdout == b"helper-secret"
    assert stderr == b""


def test_trust_manifest_path_is_reserved_from_worker_delivery():
    assert GitManager._daemon_bookkeeping_paths(
        ".github/agent-queue-integration.json\0.github/agent-queue-integration.example.json\0"
    ) == [".github/agent-queue-integration.json"]
