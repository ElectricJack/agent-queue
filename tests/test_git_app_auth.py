from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path

import pytest

from src.git.askpass_fd import answer_prompt
from src.git.askpass_broker import make_request_channel
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


def test_askpass_username_and_invalid_prompts_do_not_send_broker_requests():
    broker, request = make_request_channel()
    broker.setblocking(False)
    try:
        authority = "https://x-access-token@github.com"
        assert answer_prompt(
            "Username for 'https://github.com': ",
            request.fileno(),
            "x-access-token",
            authority,
            "https://github.com/acme/widgets.git",
        ) == "x-access-token"
        assert answer_prompt(
            "Password for 'https://attacker.example': ",
            request.fileno(),
            "x-access-token",
            authority,
            "https://github.com/acme/widgets.git",
        ) == ""
        with pytest.raises(BlockingIOError):
            broker.recv(1)
    finally:
        broker.close()
        request.close()


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


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _assert_no_broker_tasks() -> None:
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and "serve_one_credential" in task.get_coro().__qualname__
    ]


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
    open_fds_before = _open_fd_count()
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
    await asyncio.sleep(0)
    assert _open_fd_count() == open_fds_before
    _assert_no_broker_tasks()


@pytest.mark.asyncio
async def test_app_push_spawn_failure_closes_broker_reader_before_waiting(tmp_path):
    checkout, target, _, base, tip = _git_push_case(tmp_path)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(tmp_path / "missing-git")
    open_fds_before = _open_fd_count()

    with pytest.raises(GitError, match="authenticated Git push failed"):
        await manager._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=target.as_uri(),
            token="x" * (2 * 1024 * 1024),
            tip_oid=tip,
            branch="main",
            expected_old_oid=base,
        )
    await asyncio.sleep(0)
    assert _open_fd_count() == open_fds_before
    _assert_no_broker_tasks()


@pytest.mark.asyncio
async def test_broker_timeout_closes_request_channel_without_waiting_for_eof(tmp_path):
    checkout, target, _, base, tip = _git_push_case(tmp_path)
    helper_output = tmp_path / "late-helper-output"
    late_git = tmp_path / "late-git"
    late_git.write_text(
        "#!/bin/sh\n"
        "python3 -c 'import time; time.sleep(0.1)'\n"
        f'"$GIT_ASKPASS" "Password for \'https://x-access-token@github.com\': " '
        f"> {helper_output}\n"
        "exit 0\n"
    )
    late_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(late_git)
    manager._APP_CREDENTIAL_BROKER_TIMEOUT = 0.02
    manager._GIT_TIMEOUT = 2
    open_fds_before = _open_fd_count()

    result = await manager._apush_oid_with_app_auth_to_url(
        str(checkout),
        destination_url=target.as_uri(),
        token="never-released-token",
        tip_oid=tip,
        branch="main",
        expected_old_oid=base,
    )

    assert result == tip
    assert helper_output.read_bytes() == b""
    await asyncio.sleep(0)
    assert _open_fd_count() == open_fds_before
    _assert_no_broker_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["SO_PASSCRED", "SCM_RIGHTS"])
async def test_unsupported_credential_broker_fails_closed(
    tmp_path, monkeypatch, capability
):
    import src.git.askpass_broker as broker_module

    checkout, target, _, base, tip = _git_push_case(tmp_path)
    monkeypatch.delattr(broker_module.socket, capability)
    manager = GitManager()
    open_fds_before = _open_fd_count()

    with pytest.raises(GitError, match="credential broker is unavailable"):
        await manager._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=target.as_uri(),
            token="unsupported-capability-token",
            tip_oid=tip,
            branch="main",
            expected_old_oid=base,
        )

    assert _open_fd_count() == open_fds_before
    _assert_no_broker_tasks()


@pytest.mark.asyncio
async def test_descendant_cannot_drain_or_retain_askpass_credential_channel(
    tmp_path, monkeypatch
):
    """Break: an arbitrary Git descendant drains a pre-populated token FD."""
    checkout, target, _, base, tip = _git_push_case(tmp_path)
    captured = tmp_path / "descendant-capture"
    helper_output = tmp_path / "helper-output"
    environment_capture = tmp_path / "credentialed-environment"
    argument_capture = tmp_path / "credentialed-arguments"
    pids = tmp_path / "process-pids"
    helper_path = Path(answer_prompt.__code__.co_filename).resolve()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import array, os, select, socket\n"
        "fd = int(os.environ.get('AQ_GIT_APP_TOKEN_FD', "
        "os.environ.get('AQ_GIT_APP_REQUEST_FD')))\n"
        "try:\n"
        "    channel = socket.socket(fileno=os.dup(fd))\n"
        "    reply, broker_reply = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "    authority = os.environ['AQ_GIT_APP_AUTHORITY']\n"
        "    repository = os.environ['AQ_GIT_APP_REPOSITORY']\n"
        "    prompt = \"Password for 'https://x-access-token@github.com': \"\n"
        "    payload = b'aq.git-app-askpass.v1\\0' + b'\\0'.join(\n"
        "        value.encode() for value in (authority, repository, prompt))\n"
        "    rights = array.array('i', [broker_reply.fileno()])\n"
        "    channel.send(b'malformed-request-without-reply-fd')\n"
        "    channel.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])\n"
        "    broker_reply.close()\n"
        "    reply.settimeout(0.5)\n"
        "    stolen = reply.recv(1048576)\n"
        "except OSError:\n"
        "    ready, _, _ = select.select([fd], [], [], 0.2)\n"
        "    stolen = os.read(fd, 1048576) if ready else b''\n"
        f"open({str(captured)!r}, 'wb').write(stolen)\n"
    )
    username_output = tmp_path / "username-output"
    second_output = tmp_path / "second-output"
    fake_git = tmp_path / "descendant-git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "python3 -c 'import time; time.sleep(300)' &\n"
        "retainer=$!\n"
        f"printf '%s %s' \"$$\" \"$retainer\" > {pids}\n"
        f"tr '\\0' '\\n' < /proc/$$/cmdline > {argument_capture}\n"
        f"env > {environment_capture}\n"
        f'python3 {probe} {helper_path} "Password for '
        "'https://x-access-token@github.com': \"\n"
        f'"$GIT_ASKPASS" "Username for \'https://github.com\': " > {username_output}\n'
        f'"$GIT_ASKPASS" "Password for \'https://x-access-token@github.com\': " '
        f"> {helper_output}\n"
        f'"$GIT_ASKPASS" "Password for \'https://x-access-token@github.com\': " '
        f"> {second_output}\n"
        "exit 0\n"
    )
    fake_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(fake_git)
    secret = "installation-token-sentinel"
    daemon_secret = "unrelated-daemon-secret-sentinel"
    monkeypatch.setenv("DAEMON_SECRET", daemon_secret)
    open_fds_before = _open_fd_count()
    leader = None
    try:
        result = await asyncio.wait_for(
            manager._apush_oid_with_app_auth_to_url(
                str(checkout),
                destination_url=target.as_uri(),
                token=secret,
                tip_oid=tip,
                branch="main",
                expected_old_oid=base,
            ),
            timeout=3,
        )
        leader, descendant = (int(value) for value in pids.read_text().split())
        assert result == tip
        assert captured.read_bytes() == b""
        assert username_output.read_text() == "x-access-token"
        assert helper_output.read_text() == secret
        assert second_output.read_bytes() == b""
        assert secret not in environment_capture.read_text()
        assert daemon_secret not in environment_capture.read_text()
        assert secret not in argument_capture.read_text()
        assert daemon_secret not in argument_capture.read_text()
        assert not _process_group_exists(leader)
        assert not Path(f"/proc/{descendant}").exists()
        await asyncio.sleep(0)
        assert _open_fd_count() == open_fds_before
        _assert_no_broker_tasks()
    finally:
        if leader is None and pids.exists():
            leader = int(pids.read_text().split()[0])
        if leader is not None:
            try:
                os.killpg(leader, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_askpass_helper_username_is_local_with_inherited_request_fd_only():
    helper = Path(answer_prompt.__code__.co_filename)
    broker, request = make_request_channel()
    broker.setblocking(False)
    try:
        env = {
            "AQ_GIT_APP_REQUEST_FD": str(request.fileno()),
            "AQ_GIT_APP_USERNAME": "x-access-token",
            "AQ_GIT_APP_AUTHORITY": "https://x-access-token@github.com",
            "AQ_GIT_APP_REPOSITORY": "https://github.com/acme/widgets.git",
        }
        process = await asyncio.create_subprocess_exec(
            str(helper),
            "Username for 'https://github.com': ",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            pass_fds=(request.fileno(),),
        )
        stdout, stderr = await process.communicate()
    finally:
        request.close()

    assert process.returncode == 0
    assert stdout == b"x-access-token"
    assert stderr == b""
    with pytest.raises(BlockingIOError):
        broker.recv(1)
    broker.close()


def test_trust_manifest_path_is_reserved_from_worker_delivery():
    assert GitManager._daemon_bookkeeping_paths(
        ".github/agent-queue-integration.json\0.github/agent-queue-integration.example.json\0"
    ) == [".github/agent-queue-integration.json"]
