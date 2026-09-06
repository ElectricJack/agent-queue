from __future__ import annotations

import asyncio
import os
import signal
import ssl
import subprocess
from pathlib import Path

import pytest

import src.git.manager as manager_module
from src.git.askpass_fd import MAX_REQUEST_BYTES, answer_prompt
from src.git.askpass_broker import make_request_channel, serve_one_credential
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import APP_AUTH_PUSH_TIMEOUT_SECONDS, GitError, GitManager


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
async def test_exact_fetch_broker_settlement_preserves_caller_cancellation():
    entered_settlement = asyncio.Event()
    reaped = asyncio.Event()

    async def stubborn_broker():
        try:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                entered_settlement.set()
                await asyncio.Future()
        finally:
            reaped.set()

    broker = asyncio.create_task(stubborn_broker())
    settlement = asyncio.create_task(GitManager._settle_app_credential_broker(broker))
    await asyncio.wait_for(entered_settlement.wait(), timeout=1)
    settlement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(settlement, timeout=1)
    assert broker.done()
    assert reaped.is_set()


@pytest.mark.asyncio
async def test_app_exact_fetch_imports_only_requested_oid_to_daemon_namespace(tmp_path):
    checkout, source, _trap, _base, tip = _git_push_case(tmp_path)
    _git(["push", str(source), f"{tip}:refs/heads/topic"], checkout)
    destination = tmp_path / "retained.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(destination)],
        check=True,
        capture_output=True,
    )

    result = await GitManager()._afetch_exact_oid_with_app_auth_to_url(
        str(destination),
        destination_url=source.as_uri(),
        token="local-test-token",
        oid=tip,
        destination_ref="refs/aq/exact/test-tip",
    )

    assert result == tip
    assert _git(["rev-parse", "refs/aq/exact/test-tip"], destination) == tip
    assert _git(["for-each-ref", "--format=%(refname)"], destination).splitlines() == [
        "refs/aq/exact/test-tip"
    ]


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
        f'echo "$DAEMON_SECRET" > {hook_capture}\n'
        'test -z "$AQ_GIT_APP_TOKEN_FD" || cat "/proc/$$/fd/$AQ_GIT_APP_TOKEN_FD" '
        f">> {hook_capture}\n"
    )
    hook.chmod(0o700)
    global_config = tmp_path / "malicious-global"
    global_config.write_text(
        f'[url "{trap.as_uri()}"]\n\tinsteadOf = {target.as_uri()}\n'
        f"[credential]\n\thelper = !echo invoked > {helper_capture}\n"
        "[http]\n\tproxy = http://127.0.0.1:1\n"
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
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/main"], cwd=trap, capture_output=True
        ).returncode
        != 0
    )
    assert not hook_capture.exists()
    assert not helper_capture.exists()


@pytest.mark.asyncio
async def test_isolated_app_delete_uses_exact_old_lease(tmp_path):
    checkout, target, _, _base, tip = _git_push_case(tmp_path)
    manager = GitManager()
    _git(["push", str(target), f"{tip}:refs/heads/integration/test"], checkout)

    result = await manager._apush_oid_with_app_auth_to_url(
        str(checkout),
        destination_url=target.as_uri(),
        token="installation-token-sentinel",
        tip_oid=None,
        branch="integration/test",
        expected_old_oid=tip,
    )

    assert result == tip
    assert subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/integration/test"],
        cwd=target,
        capture_output=True,
    ).returncode != 0


@pytest.mark.asyncio
async def test_isolated_app_delete_refuses_moved_remote(tmp_path):
    checkout, target, _, _base, tip = _git_push_case(tmp_path)

    with pytest.raises(GitError, match="authenticated Git push failed"):
        await GitManager()._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=target.as_uri(),
            token="installation-token-sentinel",
            tip_oid=None,
            branch="main",
            expected_old_oid=tip,
        )

    assert subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/main"],
        cwd=target,
        capture_output=True,
    ).returncode == 0


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
async def test_app_push_authority_deadline_can_only_tighten_public_transport_budget(
    tmp_path, monkeypatch
):
    manager = GitManager()
    captured_deadlines = []

    async def fake_isolated_push(_checkout_path, **kwargs):
        captured_deadlines.append(kwargs["_deadline"])
        return kwargs["tip_oid"]

    monkeypatch.setattr(manager, "_apush_oid_with_app_auth_to_url", fake_isolated_push)
    loop = asyncio.get_running_loop()
    inherited_deadline = loop.time() + 30.0
    result = await manager.apush_oid_with_app_auth(
        str(tmp_path),
        repository=GitHubRepositoryBinding(303, "acme/widgets"),
        token="deadline-token",
        tip_oid="a" * 40,
        branch="main",
        expected_old_oid="b" * 40,
        authority_deadline=inherited_deadline,
    )
    assert result == "a" * 40
    assert captured_deadlines == [inherited_deadline]

    entered_at = loop.time()
    await manager.apush_oid_with_app_auth(
        str(tmp_path),
        repository=GitHubRepositoryBinding(303, "acme/widgets"),
        token="deadline-token",
        tip_oid="a" * 40,
        branch="main",
        expected_old_oid="b" * 40,
        authority_deadline=entered_at + APP_AUTH_PUSH_TIMEOUT_SECONDS * 10,
    )
    assert captured_deadlines[-1] <= loop.time() + APP_AUTH_PUSH_TIMEOUT_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf")])
async def test_app_push_rejects_nonfinite_authority_deadline(tmp_path, deadline):
    with pytest.raises(GitError, match="authority deadline"):
        await GitManager().apush_oid_with_app_auth(
            str(tmp_path),
            repository=GitHubRepositoryBinding(303, "acme/widgets"),
            token="deadline-token",
            tip_oid="a" * 40,
            branch="main",
            expected_old_oid="b" * 40,
            authority_deadline=deadline,
        )


@pytest.mark.asyncio
async def test_app_push_expired_authority_deadline_never_enters_transport(
    tmp_path, monkeypatch
):
    manager = GitManager()
    entered = False

    async def forbidden_transport(*_args, **_kwargs):
        nonlocal entered
        entered = True
        raise AssertionError("expired authority entered transport")

    monkeypatch.setattr(manager, "_apush_oid_with_app_auth_to_url", forbidden_transport)
    with pytest.raises(GitError, match="authority deadline expired"):
        await manager.apush_oid_with_app_auth(
            str(tmp_path),
            repository=GitHubRepositoryBinding(303, "acme/widgets"),
            token="deadline-token",
            tip_oid="a" * 40,
            branch="main",
            expected_old_oid="b" * 40,
            authority_deadline=asyncio.get_running_loop().time() - 1.0,
        )
    assert entered is False


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
        assert (
            answer_prompt(
                "Username for 'https://github.com': ",
                request.fileno(),
                "x-access-token",
                authority,
                "https://github.com/acme/widgets.git",
            )
            == "x-access-token"
        )
        assert (
            answer_prompt(
                "Password for 'https://attacker.example': ",
                request.fileno(),
                "x-access-token",
                authority,
                "https://github.com/acme/widgets.git",
            )
            == ""
        )
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


def _recording_zeroize(monkeypatch):
    observed = []
    real_zeroize = manager_module.zeroize

    def record(buffer):
        real_zeroize(buffer)
        observed.append(bytes(buffer))

    monkeypatch.setattr(manager_module, "zeroize", record)
    return observed


@pytest.mark.asyncio
async def test_app_push_aggregate_budget_exhaustion_during_prep_never_starts_remote(
    tmp_path, monkeypatch
):
    checkout, _target, _trap, base, tip = _git_push_case(tmp_path)
    remote_started = tmp_path / "remote-started"
    remote_git = tmp_path / "remote-git"
    remote_git.write_text(f"#!/bin/sh\ntouch {remote_started}\nexit 0\n")
    remote_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(remote_git)
    manager._GIT_TIMEOUT = 0.12
    monkeypatch.setattr(manager_module, "APP_AUTH_PUSH_TIMEOUT_SECONDS", 0.12, raising=False)
    monkeypatch.setattr(manager_module, "APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS", 0.5, raising=False)
    original_import = manager._run_isolated_import_git

    async def delayed_import(args, **kwargs):
        await asyncio.sleep(0.04)
        return await original_import(args, **kwargs)

    monkeypatch.setattr(manager, "_run_isolated_import_git", delayed_import)
    observed = _recording_zeroize(monkeypatch)
    open_fds_before = _open_fd_count()
    started_at = asyncio.get_running_loop().time()

    with pytest.raises(GitError):
        await asyncio.wait_for(
            manager.apush_oid_with_app_auth(
                str(checkout),
                repository=GitHubRepositoryBinding(303, "acme/widgets"),
                token="aggregate-exhaustion-token",
                tip_oid=tip,
                branch="main",
                expected_old_oid=base,
            ),
            timeout=1.0,
        )

    assert asyncio.get_running_loop().time() - started_at < 0.75
    assert not remote_started.exists()
    assert observed == [b""]
    assert _open_fd_count() == open_fds_before
    _assert_no_broker_tasks()


@pytest.mark.asyncio
async def test_app_push_partial_prep_leaves_one_remaining_budget_for_remote(
    tmp_path, monkeypatch
):
    checkout, target, _trap, base, tip = _git_push_case(tmp_path)
    manager = GitManager()
    manager._GIT_TIMEOUT = 0.75
    monkeypatch.setattr(manager_module, "APP_AUTH_PUSH_TIMEOUT_SECONDS", 0.75, raising=False)
    monkeypatch.setattr(manager_module, "APP_AUTH_PUSH_CLEANUP_MARGIN_SECONDS", 0.5, raising=False)
    original_import = manager._run_isolated_import_git
    original_spawn = asyncio.create_subprocess_exec
    remote_starts = 0

    async def delayed_import(args, **kwargs):
        await asyncio.sleep(0.03)
        return await original_import(args, **kwargs)

    async def counting_spawn(program, *args, **kwargs):
        nonlocal remote_starts
        if "push" in args:
            remote_starts += 1
        return await original_spawn(program, *args, **kwargs)

    monkeypatch.setattr(manager, "_run_isolated_import_git", delayed_import)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_spawn)
    started_at = asyncio.get_running_loop().time()

    result = await asyncio.wait_for(
        manager._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=target.as_uri(),
            token="partial-budget-token",
            tip_oid=tip,
            branch="main",
            expected_old_oid=base,
        ),
        timeout=1.25,
    )

    assert result == tip and remote_starts == 1
    assert _git(["rev-parse", "refs/heads/main"], target) == tip
    assert asyncio.get_running_loop().time() - started_at < 0.75


@pytest.mark.asyncio
async def test_source_import_failure_zeroizes_dummy_credential(tmp_path, monkeypatch):
    checkout = tmp_path / "not-a-repository"
    checkout.mkdir()
    observed = _recording_zeroize(monkeypatch)
    open_fds_before = _open_fd_count()

    with pytest.raises(GitError, match="push preparation failed"):
        await GitManager()._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=(tmp_path / "target.git").as_uri(),
            token="dummy-import-failure-token",
            tip_oid="a" * 40,
            branch="main",
            expected_old_oid="b" * 40,
        )

    assert observed == [b""]
    assert _open_fd_count() == open_fds_before
    _assert_no_broker_tasks()


@pytest.mark.asyncio
async def test_cancellation_during_source_import_zeroizes_dummy_credential(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    entered = asyncio.Event()
    observed = _recording_zeroize(monkeypatch)
    manager = GitManager()

    async def block_import(_args, *, home, deadline=None):
        assert home.is_dir()
        assert deadline is not None
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(manager, "_run_isolated_import_git", block_import)
    open_fds_before = _open_fd_count()
    task = asyncio.create_task(
        manager._apush_oid_with_app_auth_to_url(
            str(checkout),
            destination_url=(tmp_path / "target.git").as_uri(),
            token="dummy-cancelled-import-token",
            tip_oid="a" * 40,
            branch="main",
            expected_old_oid="b" * 40,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert observed == [b""]
    assert _open_fd_count() == open_fds_before
    _assert_no_broker_tasks()


@pytest.mark.asyncio
async def test_oversized_broker_request_setup_closes_and_zeroizes():
    open_fds_before = _open_fd_count()
    topology = await GitManager()._app_git_credential_topology(home=Path("/tmp"))
    broker, request = make_request_channel()
    token = bytearray(b"dummy-oversized-request-token")
    try:
        with pytest.raises(ValueError, match="askpass request is invalid"):
            await asyncio.wait_for(
                serve_one_credential(
                    broker,
                    token,
                    git_pid=os.getpid(),
                    topology=topology,
                    authority="x" * (MAX_REQUEST_BYTES + 1),
                    repository="https://github.com/acme/widgets.git",
                    prompt="Password for 'https://x-access-token@github.com': ",
                    timeout=0.1,
                ),
                timeout=0.5,
            )
        assert token == bytearray()
        assert broker.fileno() == -1
        _assert_no_broker_tasks()
    finally:
        broker.close()
        request.close()

    assert _open_fd_count() == open_fds_before


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancellation"])
async def test_app_push_timeout_or_cancellation_kills_entire_process_group(
    tmp_path, monkeypatch, cancel
):
    checkout, target, _, base, tip = _git_push_case(tmp_path)
    pid_file = tmp_path / "privileged-pids"
    hanging_git = tmp_path / "hanging-git"
    hanging_git.write_text(
        "#!/bin/sh\n"
        "sleep 300 &\n"
        "child=$!\n"
        f'printf \'%s %s\' "$$" "$child" > {pid_file}\n'
        'wait "$child"\n'
    )
    hanging_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(hanging_git)
    timeout = 0.2 if not cancel else 30
    manager._GIT_TIMEOUT = timeout
    monkeypatch.setattr(manager_module, "APP_AUTH_PUSH_TIMEOUT_SECONDS", timeout)
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
async def test_unsupported_credential_broker_fails_closed(tmp_path, monkeypatch, capability):
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
async def test_exact_helper_launched_by_fake_git_descendant_cannot_take_credential(
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
        f'printf \'%s %s\' "$$" "$retainer" > {pids}\n'
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
        assert helper_output.read_bytes() == b""
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
async def test_supported_git_https_remote_helper_is_credential_origin(tmp_path):
    requests = 0

    async def respond(reader, writer):
        nonlocal requests
        requests += 1
        await reader.readuntil(b"\r\n\r\n")
        status = b"401 Unauthorized" if requests <= 2 else b"403 Forbidden"
        authenticate = b'WWW-Authenticate: Basic realm="agent-queue"\r\n' if requests <= 2 else b""
        writer.write(
            b"HTTP/1.1 "
            + status
            + b"\r\n"
            + authenticate
            + b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    certificate = tmp_path / "localhost.crt"
    private_key = tmp_path / "localhost.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=tls)
    port = server.sockets[0].getsockname()[1]
    repository = f"https://x-access-token@127.0.0.1:{port}/acme/widgets.git"
    authority = f"https://x-access-token@127.0.0.1:{port}"
    prompt = f"Password for '{authority}': "
    helper = Path(answer_prompt.__code__.co_filename).resolve()
    broker, request = make_request_channel()
    token = bytearray(b"dummy-local-token")
    manager = GitManager()
    topology = await manager._app_git_credential_topology(home=tmp_path)
    environment = manager._app_git_environment(tmp_path)
    environment.update(
        {
            "GIT_ASKPASS": str(helper),
            "GIT_ASKPASS_REQUIRE": "force",
            "AQ_GIT_APP_REQUEST_FD": str(request.fileno()),
            "AQ_GIT_APP_USERNAME": "x-access-token",
            "AQ_GIT_APP_AUTHORITY": authority,
            "AQ_GIT_APP_REPOSITORY": repository,
        }
    )
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/git",
        "-c",
        "http.sslVerify=false",
        "ls-remote",
        repository,
        cwd=tmp_path,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        pass_fds=(request.fileno(),),
        start_new_session=True,
    )
    request.close()
    try:
        served = await asyncio.wait_for(
            serve_one_credential(
                broker,
                token,
                git_pid=process.pid,
                topology=topology,
                authority=authority,
                repository=repository,
                prompt=prompt,
                timeout=2,
            ),
            timeout=3,
        )
    finally:
        await GitManager._kill_app_git_group(process)
        server.close()
        await server.wait_closed()

    stderr = await process.stderr.read()
    assert served is True, (requests, stderr)
    assert token == bytearray()
    assert requests >= 1


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
