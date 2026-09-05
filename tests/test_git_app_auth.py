from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from src.git.askpass_fd import answer_prompt
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitError, GitManager


def _git(args: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _git_push_case(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    checkout = tmp_path / "worker-checkout"
    target = tmp_path / "intended.git"
    trap = tmp_path / "rewritten.git"
    checkout.mkdir()
    _git(["init", "--initial-branch=main"], checkout)
    _git(["config", "user.name", "Test"], checkout)
    _git(["config", "user.email", "test@example.com"], checkout)
    (checkout / "file.txt").write_text("base")
    _git(["add", "file.txt"], checkout)
    _git(["commit", "-m", "base"], checkout)
    base = _git(["rev-parse", "HEAD"], checkout)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(trap)],
        check=True,
        capture_output=True,
    )
    _git(["push", str(target), f"{base}:refs/heads/main"], checkout)
    (checkout / "file.txt").write_text("tip")
    _git(["commit", "-am", "tip"], checkout)
    tip = _git(["rev-parse", "HEAD"], checkout)
    return checkout, target, trap, base, tip


@pytest.mark.asyncio
async def test_privileged_push_ignores_worker_hooks_config_rewrites_helpers_and_daemon_env(
    tmp_path, monkeypatch
):
    checkout, target, trap, base, tip = _git_push_case(tmp_path)
    shared_checkout = tmp_path / "worker-shared-checkout"
    subprocess.run(
        ["git", "clone", "--shared", str(checkout), str(shared_checkout)],
        check=True,
        capture_output=True,
    )
    checkout = shared_checkout
    replacement = _git(
        ["commit-tree", _git(["rev-parse", f"{tip}^{{tree}}"], checkout), "-p", base],
        checkout,
    )
    _git(["replace", tip, replacement], checkout)
    assert (checkout / ".git" / "objects" / "info" / "alternates").is_file()
    assert _git(["cat-file", "-p", tip], checkout) == _git(
        ["cat-file", "-p", replacement], checkout
    )
    hook_capture = tmp_path / "hook-capture"
    helper_capture = tmp_path / "helper-capture"
    hooks = checkout / ".git" / "hooks"
    hook = hooks / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        f"echo \"$DAEMON_SECRET\" > {hook_capture}\n"
        'test -z "$AQ_GIT_APP_TOKEN_FD" || cat "/proc/$$/fd/$AQ_GIT_APP_TOKEN_FD" '
        f">> {hook_capture}\n"
    )
    hook.chmod(0o700)
    global_config = tmp_path / "malicious-global"
    global_config.write_text(
        f'[url "{trap.as_uri()}"]\n\tinsteadOf = {target.as_uri()}\n'
        f'[credential]\n\thelper = !echo invoked > {helper_capture}\n'
        '[http]\n\tproxy = http://127.0.0.1:1\n'
    )
    _git(["config", "url.file:///unrelated.invalid/.insteadOf", target.as_uri()], checkout)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(global_config))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("DAEMON_SECRET", "unrelated-daemon-sentinel")

    result = await GitManager()._apush_oid_with_app_auth_to_url(
        str(checkout),
        destination_url=target.as_uri(),
        token="installation-token-sentinel",
        tip_oid=tip,
        branch="main",
        expected_old_oid=base,
    )

    assert result == tip
    assert _git(["rev-parse", "refs/heads/main"], target) == tip
    assert subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/main"], cwd=trap, capture_output=True
    ).returncode != 0
    assert not hook_capture.exists()
    assert not helper_capture.exists()


@pytest.mark.asyncio
async def test_app_push_uses_frozen_repository_and_one_shot_fd_without_secret_leak(
    tmp_path, monkeypatch
):
    secret = "token-with-unbounded-length-and-a-sentinel"
    captured = {}
    manager = GitManager()

    async def fake_isolated_push(checkout_path, **kwargs):
        captured.update(checkout_path=checkout_path, **kwargs)
        return kwargs["tip_oid"]

    monkeypatch.setattr(manager, "_apush_oid_with_app_auth_to_url", fake_isolated_push)
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token")

    result = await manager.apush_oid_with_app_auth(
        str(tmp_path / "checkout"),
        repository=GitHubRepositoryBinding(303, "acme/widgets"),
        token=secret,
        tip_oid="a" * 40,
        branch="main",
        expected_old_oid="b" * 40,
    )

    assert result == "a" * 40
    assert captured["destination_url"] == "https://github.com/acme/widgets.git"
    assert captured["branch"] == "main"
    assert captured["expected_old_oid"] == "b" * 40


@pytest.mark.asyncio
async def test_app_push_error_never_repeats_remote_output_or_token(tmp_path):
    secret = "high-authority-secret"
    checkout, target, _, base, tip = _git_push_case(tmp_path)
    failed_git = tmp_path / "failed-git"
    failed_git.write_text(f"#!/bin/sh\necho 'remote echoed {secret}' >&2\nexit 1\n")
    failed_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(failed_git)
    with pytest.raises(GitError) as caught:
        await manager._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=target.as_uri(),
            token=secret,
            tip_oid=tip,
            branch="main",
            expected_old_oid=base,
        )
    assert secret not in str(caught.value)
    assert "remote echoed" not in str(caught.value)


def test_askpass_reads_token_only_for_password_prompt():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"secret")
    os.close(write_fd)
    try:
        authority = "https://x-access-token@github.com"
        assert answer_prompt(
            "Username for 'https://github.com': ", read_fd, "x-access-token", authority
        ) == "x-access-token"
        assert answer_prompt(
            "Password for 'https://attacker.example': ", read_fd, "x-access-token", authority
        ) == ""
        assert answer_prompt(
            "Password for 'https://x-access-token@github.com': ",
            read_fd,
            "x-access-token",
            authority,
        ) == "secret"
        assert answer_prompt(
            "Password for 'https://x-access-token@github.com': ",
            read_fd,
            "x-access-token",
            authority,
        ) == ""
    finally:
        os.close(read_fd)


async def _wait_for_file(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancellation"])
async def test_app_push_timeout_or_cancellation_kills_entire_process_group(
    tmp_path, cancel
):
    checkout, target, _, base, tip = _git_push_case(tmp_path)
    pid_file = tmp_path / "privileged-pids"
    hanging_git = tmp_path / "hanging-git"
    hanging_git.write_text(
        "#!/bin/sh\n"
        "sleep 300 &\n"
        "child=$!\n"
        f"printf '%s %s' \"$$\" \"$child\" > {pid_file}\n"
        "wait \"$child\"\n"
    )
    hanging_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(hanging_git)
    manager._GIT_TIMEOUT = 0.2 if not cancel else 30
    task = asyncio.create_task(
        manager._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=target.as_uri(),
            token="x" * (2 * 1024 * 1024),
            tip_oid=tip,
            branch="main",
            expected_old_oid=base,
        )
    )
    await _wait_for_file(pid_file)
    leader, descendant = (int(value) for value in pid_file.read_text().split())
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(GitError, match="authenticated Git push failed"):
            await task

    for _ in range(200):
        if not _process_group_exists(leader):
            break
        await asyncio.sleep(0.01)
    assert not _process_group_exists(leader)
    assert not Path(f"/proc/{descendant}").exists()


@pytest.mark.asyncio
async def test_app_push_spawn_failure_closes_broker_reader_before_waiting(tmp_path):
    checkout, target, _, base, tip = _git_push_case(tmp_path)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(tmp_path / "missing-git")

    with pytest.raises(GitError, match="authenticated Git push failed"):
        await manager._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=target.as_uri(),
            token="x" * (2 * 1024 * 1024),
            tip_oid=tip,
            branch="main",
            expected_old_oid=base,
        )


@pytest.mark.asyncio
async def test_askpass_helper_is_directly_executable_with_inherited_fd_only():
    helper = Path(answer_prompt.__code__.co_filename)
    token_fd, write_fd = os.pipe()
    os.write(write_fd, b"helper-secret")
    os.close(write_fd)
    try:
        env = {
            "AQ_GIT_APP_TOKEN_FD": str(token_fd),
            "AQ_GIT_APP_USERNAME": "x-access-token",
            "AQ_GIT_APP_AUTHORITY": "https://x-access-token@github.com",
        }
        process = await asyncio.create_subprocess_exec(
            str(helper),
            "Password for 'https://x-access-token@github.com': ",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            pass_fds=(token_fd,),
        )
        stdout, stderr = await process.communicate()
    finally:
        os.close(token_fd)

    assert process.returncode == 0
    assert stdout == b"helper-secret"
    assert stderr == b""


def test_trust_manifest_path_is_reserved_from_worker_delivery():
    assert GitManager._daemon_bookkeeping_paths(
        ".github/agent-queue-integration.json\0.github/agent-queue-integration.example.json\0"
    ) == [".github/agent-queue-integration.json"]
