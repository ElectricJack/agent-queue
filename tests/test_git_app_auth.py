from __future__ import annotations

import asyncio
import base64
import logging
import os
import signal
import ssl
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock

import pytest

import src.git.manager as manager_module
from src.config import WorktreesConfig
from src.git.askpass_fd import MAX_REQUEST_BYTES, answer_prompt
from src.git.askpass_broker import make_request_channel, serve_one_credential
from src.git.github_app import GitHubRepositoryBinding
from src.git.github_contracts import GitHubCredentialMode
from src.git.manager import (
    APP_AUTH_PUSH_TIMEOUT_SECONDS,
    GitError,
    GitManager,
    PullRequestIdentity,
    RemoteRefState,
)
from src.integration.development import DevelopmentIntegration
from src.orchestrator.worktree_manager import WorktreeSlotManager


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


def _local_tls_context(tmp_path: Path) -> ssl.SSLContext:
    certificate = tmp_path / "localhost.crt"
    private_key = tmp_path / "localhost.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-keyout", str(private_key), "-out", str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    return tls


class _GitHubStandIn:
    """Answer ``https://github.com/<owner>/<repo>.git`` from a local bare repository.

    Git keeps the literal github.com URL, and with it the prompt and remote
    helper arguments the credential broker pins; only an ``http.proxy`` in the
    test's own Git arguments routes the CONNECT tunnel here.  The tunnel is
    terminated with a throwaway certificate and served by ``git http-backend``.
    With ``required_token`` set, every request without that App credential is
    answered 401, as GitHub answers a private repository.
    """

    def __init__(self, tmp_path: Path, root: Path, *, required_token: str | None) -> None:
        self.tmp_path = tmp_path
        self.root = root
        self.expected = None if required_token is None else "Basic " + base64.b64encode(
            f"x-access-token:{required_token}".encode()
        ).decode()
        self.tunnels: list[str] = []
        self.authorizations: list[str | None] = []
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def __aenter__(self) -> Self:
        tls = _local_tls_context(self.tmp_path)

        async def tunnel(reader, writer):
            try:
                target = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0].decode()
                self.tunnels.append(target)
                if not target.startswith("CONNECT github.com:443 "):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    return
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.start_tls(tls)
                while await self._answer(reader, writer):
                    pass
            except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
                pass
            finally:
                writer.close()

        self._server = await asyncio.start_server(tunnel, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def git_options(self) -> list[str]:
        return ["-c", f"http.proxy=http://127.0.0.1:{self.port}", "-c", "http.sslVerify=false"]

    async def _answer(self, reader, writer) -> bool:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return False
        request, *lines = head.decode("latin-1").split("\r\n")
        method, target, _version = request.split(" ", 2)
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines if line)
        }
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        authorization = headers.get("authorization")
        self.authorizations.append(authorization)
        if self.expected is not None and authorization != self.expected:
            writer.write(
                b"HTTP/1.1 401 Unauthorized\r\n"
                b'WWW-Authenticate: Basic realm="GitHub"\r\nContent-Length: 0\r\n\r\n'
            )
            await writer.drain()
            return True
        path, _, query = target.partition("?")
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": "127.0.0.1",
        }
        if "git-protocol" in headers:
            environment["GIT_PROTOCOL"] = headers["git-protocol"]
        if "content-encoding" in headers:
            environment["HTTP_CONTENT_ENCODING"] = headers["content-encoding"]
        backend = await asyncio.create_subprocess_exec(
            "git", "http-backend", env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        output, _ = await backend.communicate(body)
        cgi_head, _, payload = output.partition(b"\r\n\r\n")
        status = b"200 OK"
        response_headers = []
        for line in cgi_head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.lower() == b"status":
                status = value.strip()
            elif line:
                response_headers.append(line + b"\r\n")
        writer.write(
            b"HTTP/1.1 " + status + b"\r\n" + b"".join(response_headers)
            + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
        )
        await writer.drain()
        return True


def _github_stand_in_case(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """``acme/widgets`` under a stand-in root, and a checkout one branch and tag behind."""
    work = tmp_path / "work"
    root = tmp_path / "github"
    source = root / "acme" / "widgets.git"
    destination = tmp_path / "destination"
    work.mkdir()
    _git(["init", "--initial-branch=main"], work)
    _git(["config", "user.name", "Test"], work)
    _git(["config", "user.email", "test@example.com"], work)
    (work / "file.txt").write_text("base")
    _git(["add", "file.txt"], work)
    _git(["commit", "-m", "base"], work)
    source.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(source)],
        check=True, capture_output=True,
    )
    _git(["push", str(source), "main:refs/heads/main"], work)
    _git(["clone", str(source), str(destination)], tmp_path)
    _git(["config", "remote.origin.url", "https://github.com/acme/widgets.git"], destination)
    (work / "file.txt").write_text("topic")
    _git(["commit", "-am", "topic"], work)
    tip = _git(["rev-parse", "HEAD"], work)
    _git(["push", str(source), "HEAD:refs/heads/topic", "HEAD:refs/tags/archive/topic"], work)
    return work, source, destination, tip


def _route_app_git_through(manager: GitManager, stand_in: _GitHubStandIn, monkeypatch) -> None:
    """Prefix the stand-in's proxy options to each privileged Git invocation."""
    original = manager._arun_authenticated_git_once

    async def through_stand_in(args, **kwargs):
        return await original([*stand_in.git_options(), *args], **kwargs)

    monkeypatch.setattr(manager, "_arun_authenticated_git_once", through_stand_in)


class _BoundAppAccess:
    def __init__(
        self,
        *,
        token: str | None = "app-token",
        mode: GitHubCredentialMode = GitHubCredentialMode.APP,
    ) -> None:
        self.auth = type("Auth", (), {"mode": mode})()
        self.token = token
        self.requested: list[str] = []
        self.token_requests: list[GitHubRepositoryBinding] = []

    async def bind_repository(self, reference: str) -> GitHubRepositoryBinding:
        self.requested.append(reference)
        return GitHubRepositoryBinding(303, "acme/widgets")

    async def installation_token(self, repository: GitHubRepositoryBinding) -> str | None:
        self.token_requests.append(repository)
        return self.token

    def validate_repository_reference(
        self, repository: GitHubRepositoryBinding, reference: str
    ) -> None:
        assert repository.full_name == "acme/widgets"
        if reference not in {
            "https://github.com/acme/widgets.git", "git@github.com:acme/widgets.git"
        }:
            raise GitError("GitHub repository reference did not match the authorized repository")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reference", ["git@github.com:acme/widgets.git", "acme/widgets"]
)
async def test_app_clone_derives_https_from_verified_identity(tmp_path, monkeypatch, reference):
    access = _BoundAppAccess()
    manager = GitManager(github_access=access)
    captured = {}

    async def capture(configured_url, checkout_path, **kwargs):
        captured.update(configured_url=configured_url, checkout_path=checkout_path, **kwargs)

    monkeypatch.setattr(manager, "_aclone_with_auth_to_url", capture)
    await manager.acreate_checkout(reference, str(tmp_path / "checkout"))

    assert access.requested == [reference]
    assert len(access.token_requests) == 1
    assert captured["source_url"] == "https://github.com/acme/widgets.git"
    assert captured["configured_url"] == reference
    assert captured["token"] == "app-token"


@pytest.mark.asyncio
async def test_app_clone_never_falls_back_when_token_is_missing(tmp_path, monkeypatch):
    manager = GitManager(github_access=_BoundAppAccess(token=None))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("ambient Git was used after App credential failure")

    monkeypatch.setattr(manager, "_arun", forbidden)
    with pytest.raises(GitError, match="App credential is unavailable"):
        await manager.acreate_checkout("git@github.com:acme/widgets.git", str(tmp_path / "checkout"))


@pytest.mark.asyncio
async def test_app_configuration_keeps_local_clone_local(tmp_path):
    _checkout, source, _trap, base, _tip = _git_push_case(tmp_path)
    access = _BoundAppAccess()
    destination = tmp_path / "local-checkout"

    await GitManager(github_access=access).acreate_checkout(str(source), str(destination))

    assert access.requested == []
    assert _git(["rev-parse", "HEAD"], destination) == base


@pytest.mark.asyncio
async def test_validated_push_uses_bound_app_destination_and_exact_creation_lease(
    tmp_path, monkeypatch
):
    checkout, _source, _trap, _base, tip = _git_push_case(tmp_path)
    _git(["remote", "add", "origin", "git@github.com:acme/widgets.git"], checkout)
    access = _BoundAppAccess()
    manager = GitManager(github_access=access)
    observed = AsyncMock(side_effect=[None, tip])
    monkeypatch.setattr(manager, "_aobserved_remote_head", observed)
    transferred = []

    async def capture(checkout_path, **kwargs):
        transferred.append((checkout_path, kwargs))
        return kwargs["tip_oid"]

    monkeypatch.setattr(manager, "_apush_oid_with_app_auth_to_url", capture)
    await manager.apush_validated_ref(str(checkout), "HEAD", "task/app")

    assert access.requested == ["git@github.com:acme/widgets.git"]
    assert access.token_requests == [GitHubRepositoryBinding(303, "acme/widgets")]
    assert transferred[0][1]["destination_url"] == "https://github.com/acme/widgets.git"
    assert transferred[0][1]["token"] == "app-token"
    assert transferred[0][1]["tip_oid"] == tip
    assert transferred[0][1]["expected_old_oid"] == "0" * 40


@pytest.mark.asyncio
async def test_app_push_missing_token_never_uses_checkout_transport(tmp_path, monkeypatch):
    checkout, _source, _trap, _base, _tip = _git_push_case(tmp_path)
    _git(["remote", "add", "origin", "git@github.com:acme/widgets.git"], checkout)
    manager = GitManager(github_access=_BoundAppAccess(token=None))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("push transport ran without an App token")

    monkeypatch.setattr(manager, "_atransfer_exact_ref", forbidden)
    with pytest.raises(GitError, match="App credential is unavailable"):
        await manager.apush_validated_ref(str(checkout), "HEAD", "task/app")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,token", [
    (GitHubCredentialMode.APP, "app-token"),
    (GitHubCredentialMode.EXISTING_LOGIN, None),
])
async def test_remote_head_read_uses_selected_isolated_credential(
    tmp_path, monkeypatch, mode, token
):
    checkout, _source, _trap, base, _tip = _git_push_case(tmp_path)
    _git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], checkout)
    access = _BoundAppAccess(token=token, mode=mode)
    manager = GitManager(github_access=access)
    captured = []

    async def isolated(args, **kwargs):
        captured.append((args, kwargs))
        return f"{base}\trefs/heads/main\n".encode()

    monkeypatch.setattr(manager, "_arun_authenticated_git", isolated)
    monkeypatch.setattr(
        manager, "arun_git_result",
        AsyncMock(side_effect=AssertionError("checkout Git may not read GitHub refs")),
    )
    remote = await manager.als_remote_ref(str(checkout), "main")

    assert remote.state is RemoteRefState.PRESENT and remote.oid == base
    assert access.token_requests == [GitHubRepositoryBinding(303, "acme/widgets")]
    assert captured[0][1]["token"] == token
    assert captured[0][1]["repository_url"] == "https://github.com/acme/widgets.git"


@pytest.mark.asyncio
async def test_remote_head_read_does_not_fall_back_after_app_auth_failure(tmp_path, monkeypatch):
    checkout, _source, _trap, _base, _tip = _git_push_case(tmp_path)
    _git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], checkout)
    manager = GitManager(github_access=_BoundAppAccess(token=None))
    monkeypatch.setattr(
        manager, "arun_git_result",
        AsyncMock(side_effect=AssertionError("ambient Git may not run")),
    )
    remote = await manager.als_remote_ref(str(checkout), "main")
    assert remote.state is RemoteRefState.ERROR
    assert "App credential is unavailable" in (remote.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    GitHubCredentialMode.APP, GitHubCredentialMode.EXISTING_LOGIN,
])
async def test_exact_repository_transfers_select_credential_for_each_operation(
    monkeypatch, mode
):
    access = _BoundAppAccess(mode=mode)
    binding = GitHubRepositoryBinding(303, "acme/widgets")
    issued = []

    async def fresh(repository):
        issued.append(repository)
        return f"generation-{len(issued)}" if mode is GitHubCredentialMode.APP else None

    access.installation_token = fresh
    manager = GitManager(github_access=access)
    fetch = AsyncMock(return_value="a" * 40)
    push = AsyncMock(return_value="a" * 40)
    delete = AsyncMock(return_value="a" * 40)
    monkeypatch.setattr(manager, "afetch_exact_oid_with_app_auth", fetch)
    monkeypatch.setattr(manager, "apush_oid_with_app_auth", push)
    monkeypatch.setattr(manager, "adelete_ref_with_app_auth", delete)

    await manager.afetch_repository_oid(
        "/tmp/store.git", repository=binding, oid="a" * 40,
        destination_ref="refs/aq/test",
    )
    await manager.apush_repository_oid(
        "/tmp/store.git", repository=binding, tip_oid="a" * 40,
        branch="aq/test", expected_old_oid="0" * 40,
    )
    await manager.adelete_repository_ref(
        "/tmp/store.git", repository=binding, branch="aq/test",
        expected_old_oid="a" * 40,
    )

    assert issued == [binding] * 3
    expected = [f"generation-{i}" for i in range(1, 4)] if mode is GitHubCredentialMode.APP else [None] * 3
    assert [fetch.call_args.kwargs["token"], push.call_args.kwargs["token"],
            delete.call_args.kwargs["token"]] == expected


@pytest.mark.asyncio
async def test_observed_push_uses_isolated_destination_not_checkout_remote(tmp_path, monkeypatch):
    checkout, target, trap, _base, tip = _git_push_case(tmp_path)
    _git(["remote", "add", "origin", str(trap)], checkout)
    manager = GitManager()
    monkeypatch.setattr(
        manager, "_apush_destination", AsyncMock(return_value=(target.as_uri(), "dummy-token"))
    )

    assert await manager.apush_validated_ref(str(checkout), "HEAD", "main") == tip

    assert _git(["rev-parse", "refs/heads/main"], target) == tip
    assert _git(["for-each-ref", "--format=%(refname)"], trap) == ""


@pytest.mark.asyncio
async def test_existing_login_keeps_operator_ssh_clone_without_gh_binding(tmp_path, monkeypatch):
    access = _BoundAppAccess(token=None, mode=GitHubCredentialMode.EXISTING_LOGIN)
    manager = GitManager(github_access=access)
    commands = []

    async def record(args, cwd=None, timeout=None):
        commands.append(args)
        return ""

    monkeypatch.setattr(manager, "_arun", record)
    await manager.acreate_checkout("git@github.com:acme/widgets.git", str(tmp_path / "checkout"))

    assert access.requested == []
    assert commands == [["clone", "git@github.com:acme/widgets.git", str(tmp_path / "checkout")]]


@pytest.mark.asyncio
async def test_isolated_clone_preserves_configured_remote_and_ignores_global_rewrite(
    tmp_path, monkeypatch
):
    checkout, source, trap, _base, tip = _git_push_case(tmp_path)
    _git(["push", str(source), f"{tip}:refs/heads/main"], checkout)
    global_config = tmp_path / "malicious-global"
    global_config.write_text(
        f'[url "{trap.as_uri()}"]\n\tinsteadOf = {source.as_uri()}\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    destination = tmp_path / "new-checkout"
    await GitManager()._aclone_with_auth_to_url(
        "git@github.com:acme/widgets.git",
        str(destination),
        source_url=source.as_uri(),
        token="local-test-token",
    )

    assert _git(["rev-parse", "HEAD"], destination) == tip
    assert _git(["config", "--get", "remote.origin.url"], destination) == (
        "git@github.com:acme/widgets.git"
    )
    assert not (destination / ".git" / "objects" / "info" / "alternates").exists()
    assert _git(["for-each-ref", "--format=%(refname)"], trap) == ""


@pytest.mark.asyncio
async def test_isolated_bare_clone_retains_origin_without_credentials(tmp_path):
    _checkout, source, _trap, base, _tip = _git_push_case(tmp_path)
    destination = tmp_path / "retained.git"

    await GitManager()._aclone_with_auth_to_url(
        "https://github.com/acme/widgets.git", str(destination),
        source_url=source.as_uri(), token="local-test-token", bare=True,
    )

    assert _git(["rev-parse", "--is-bare-repository"], destination) == "true"
    assert _git(["rev-parse", "refs/heads/main"], destination) == base
    assert _git(["config", "--get", "remote.origin.url"], destination) == (
        "https://github.com/acme/widgets.git"
    )
    assert _git(["config", "--get", "remote.origin.fetch"], destination) == (
        "+refs/heads/*:refs/remotes/origin/*"
    )


@pytest.mark.asyncio
async def test_isolated_origin_fetch_imports_source_refs_without_using_checkout_remote(tmp_path):
    checkout, source, trap, base, tip = _git_push_case(tmp_path)
    destination = tmp_path / "destination"
    _git(["clone", str(source), str(destination)], tmp_path)
    _git(["push", str(source), f"{tip}:refs/heads/topic"], checkout)
    _git(["config", "remote.origin.url", str(trap)], destination)

    await GitManager()._afetch_origin_with_auth_to_url(
        str(destination), source_url=source.as_uri(), token="local-test-token"
    )

    assert _git(["rev-parse", "refs/remotes/origin/topic"], destination) == tip
    assert _git(["rev-parse", "HEAD"], destination) == base
    assert _git(["config", "--get", "remote.origin.url"], destination) == str(trap)
    assert _git(["for-each-ref", "--format=%(refname)"], trap) == ""


def _origin_fetch_case(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A 40-commit source carrying an incompressible 1 MiB blob, and an empty checkout."""
    work = tmp_path / "work"
    source = tmp_path / "source.git"
    destination = tmp_path / "destination"
    for path in (work, destination):
        path.mkdir()
        _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.name", "Test"], work)
    _git(["config", "user.email", "test@example.com"], work)
    (work / "large.bin").write_bytes(os.urandom(1 << 20))
    for number in range(40):
        (work / "counter.txt").write_text(f"{number}\n")
        _git(["add", "large.bin", "counter.txt"], work)
        _git(["commit", "-m", f"commit {number}"], work)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(source)],
        check=True, capture_output=True,
    )
    _git(["push", str(source), "main:refs/heads/main"], work)
    _git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], destination)
    return work, source, destination


def _commit_and_publish(work: Path, source: Path, content: str) -> str:
    (work / "counter.txt").write_text(content)
    _git(["commit", "-am", content], work)
    _git(["push", str(source), "main:refs/heads/main"], work)
    return _git(["rev-parse", "HEAD"], work)


def _object_store_snapshot(objects: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(objects)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in objects.rglob("*")
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_isolated_origin_fetch_downloads_only_what_the_checkout_lacks(tmp_path):
    work, source, destination = _origin_fetch_case(tmp_path)
    manager = GitManager()
    counted = dict(
        line.split(": ", 1) for line in _git(["count-objects", "-v"], source).splitlines()
    )
    source_objects = int(counted["count"]) + int(counted["in-pack"])

    first = await manager._afetch_origin_with_auth_to_url(
        str(destination), source_url=source.as_uri(), token="local-test-token"
    )
    tip = _commit_and_publish(work, source, "one more\n")
    second = await manager._afetch_origin_with_auth_to_url(
        str(destination), source_url=source.as_uri(), token="local-test-token"
    )

    # An empty checkout has nothing to lend, so the first fetch is the whole history.
    assert (first.mode, first.seeded_tips) == ("incremental", 0)
    assert first.objects >= source_objects > 120
    assert first.size_kib >= 1024
    # One new commit is one commit, one tree and one blob — not the 1 MiB history.
    assert (second.mode, second.seeded_tips) == ("incremental", 1)
    assert second.objects <= 5
    assert second.size_kib < 64
    assert _git(["rev-parse", "refs/remotes/origin/main"], destination) == tip
    # The checkout owns every object it now references; nothing borrows back.
    assert not (destination / ".git" / "objects" / "info" / "alternates").exists()
    _git(["fsck", "--connectivity-only", "--no-dangling"], destination)


@pytest.mark.asyncio
async def test_isolated_origin_fetch_borrows_checkout_objects_read_only(tmp_path, monkeypatch):
    work, source, destination = _origin_fetch_case(tmp_path)
    manager = GitManager()
    await manager._afetch_origin_with_auth_to_url(
        str(destination), source_url=source.as_uri(), token="local-test-token"
    )
    _commit_and_publish(work, source, "one more\n")
    objects = destination / ".git" / "objects"
    real_fetch = manager._arun_authenticated_git
    observed: dict[str, object] = {}

    async def watched_fetch(args, **kwargs):
        git_dir = Path(next(arg for arg in args if arg.startswith("--git-dir=")).split("=", 1)[1])
        observed["alternates"] = (git_dir / "objects" / "info" / "alternates").read_text()
        observed["seeded"] = sorted(
            str(path.relative_to(git_dir)) for path in (git_dir / "refs").rglob("*")
            if path.is_file()
        )
        observed["args"] = list(args)
        observed["temporary"] = git_dir.parent
        before = _object_store_snapshot(objects)
        output = await real_fetch(args, **kwargs)
        observed["unchanged"] = _object_store_snapshot(objects) == before
        return output

    monkeypatch.setattr(manager, "_arun_authenticated_git", watched_fetch)
    await manager._afetch_origin_with_auth_to_url(
        str(destination), source_url=source.as_uri(), token="local-test-token"
    )

    assert observed["alternates"] == f"{objects.resolve()}\n"
    assert observed["seeded"] == ["refs/aq/have/0"]
    # The credentialed process writes only into its own repository, and never
    # runs for-each-ref against the checkout to discover its tips.
    assert observed["unchanged"] is True
    assert "core.alternateRefsCommand=true" in observed["args"]
    assert not Path(observed["temporary"]).exists()
    assert not (objects / "info" / "alternates").exists()


@pytest.mark.asyncio
async def test_isolated_origin_fetch_keeps_token_out_of_checkout_and_logs(tmp_path, caplog):
    work, source, destination = _origin_fetch_case(tmp_path)
    token = "ghs_origin-fetch-sentinel-token"
    manager = GitManager()

    with caplog.at_level(logging.DEBUG, logger="src.git.manager"):
        await manager._afetch_origin_with_auth_to_url(
            str(destination), source_url=source.as_uri(), token=token
        )
        _commit_and_publish(work, source, "one more\n")
        await manager._afetch_origin_with_auth_to_url(
            str(destination), source_url=source.as_uri(), token=token
        )

    assert "origin_fetch=incremental, seeded_tips=1" in caplog.text
    assert token not in caplog.text
    for path in (destination / ".git").rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path
    # The seeded refs/aq/have/* names stay in the throwaway repository.
    assert _git(["for-each-ref", "--format=%(refname)"], destination).split() == [
        "refs/remotes/origin/main"
    ]


@pytest.mark.asyncio
async def test_isolated_origin_fetch_failure_names_its_mode(tmp_path, monkeypatch):
    _work, source, destination = _origin_fetch_case(tmp_path)
    manager = GitManager()

    async def timed_out(*_args, **_kwargs):
        raise GitError("authenticated Git acquisition failed: exception TimeoutError: phase=x")

    monkeypatch.setattr(manager, "_arun_authenticated_git", timed_out)
    with pytest.raises(GitError) as caught:
        await manager._afetch_origin_with_auth_to_url(
            str(destination), source_url=source.as_uri(), token="local-test-token"
        )
    assert str(caught.value) == (
        "authenticated Git acquisition failed: exception TimeoutError: phase=x; "
        "origin_fetch=incremental, seeded_tips=0"
    )


@pytest.mark.asyncio
async def test_isolated_origin_fetch_still_prunes_deleted_branches(tmp_path):
    work, source, destination = _origin_fetch_case(tmp_path)
    _git(["push", str(source), "main:refs/heads/topic"], work)
    _git(["push", str(source), "main:refs/tags/v1"], work)
    manager = GitManager()
    await manager._afetch_origin_with_auth_to_url(
        str(destination), source_url=source.as_uri(), token="local-test-token"
    )
    assert _git(["rev-parse", "refs/remotes/origin/topic"], destination)
    _git(["push", str(source), ":refs/heads/topic"], work)

    await manager._afetch_origin_with_auth_to_url(
        str(destination), source_url=source.as_uri(), token="local-test-token"
    )

    assert _git(["for-each-ref", "--format=%(refname)"], destination).split() == [
        "refs/remotes/origin/main", "refs/tags/v1"
    ]


@pytest.mark.asyncio
async def test_isolated_origin_fetch_downloads_everything_for_a_shallow_checkout(tmp_path):
    work, source, _empty = _origin_fetch_case(tmp_path)
    shallow = tmp_path / "shallow"
    _git(["clone", "--depth=1", source.as_uri(), str(shallow)], tmp_path)
    tip = _commit_and_publish(work, source, "one more\n")

    transfer = await GitManager()._afetch_origin_with_auth_to_url(
        str(shallow), source_url=source.as_uri(), token="local-test-token"
    )

    # A shallow checkout lacks the history behind its tips; naming them as held
    # would make the remote leave out objects the checkout does not have.
    assert (transfer.mode, transfer.seeded_tips) == ("full", 0)
    assert _git(["rev-parse", "refs/remotes/origin/main"], shallow) == tip


def _exact_fetch_case(tmp_path: Path) -> tuple[Path, Path, Path]:
    """The 40-commit source of ``_origin_fetch_case`` and an empty daemon-owned store."""
    work, source, _checkout = _origin_fetch_case(tmp_path)
    store = tmp_path / "retained.git"
    _git(["init", "--bare", "--template=", str(store)], tmp_path)
    return work, source, store


def _count_objects(git_dir: Path) -> tuple[int, int]:
    """Objects and KiB a repository stores itself; objects it borrows are not counted."""
    counted = dict(
        line.split(": ", 1)
        for line in _git([f"--git-dir={git_dir}", "count-objects", "-v"], git_dir).splitlines()
    )
    return (
        int(counted["count"]) + int(counted["in-pack"]),
        int(counted["size"]) + int(counted["size-pack"]),
    )


def _write_object_only(work: Path, store: Path, oid: str) -> str:
    """Copy commit *oid*'s object into *store* without its tree or parents."""
    content = subprocess.run(
        ["git", "cat-file", "commit", oid], cwd=work, capture_output=True, check=True
    ).stdout
    written = subprocess.run(
        ["git", f"--git-dir={store}", "hash-object", "-t", "commit", "-w", "--stdin"],
        input=content, capture_output=True, check=True,
    )
    return written.stdout.decode().strip()


def _watch_exact_fetches(
    manager: GitManager, monkeypatch: pytest.MonkeyPatch, *, lender: Path | None = None
) -> list[dict[str, object]]:
    """Record what each credentialed fetch saw and what it took off the network."""
    observed: list[dict[str, object]] = []
    real_fetch = manager._arun_authenticated_git

    async def watched_fetch(args, **kwargs):
        git_dir = Path(next(arg for arg in args if arg.startswith("--git-dir=")).split("=", 1)[1])
        alternates = git_dir / "objects" / "info" / "alternates"
        record: dict[str, object] = {
            "args": list(args),
            "alternates": alternates.read_text() if alternates.exists() else None,
            "seeded": sorted(
                str(path.relative_to(git_dir)) for path in (git_dir / "refs").rglob("*")
                if path.is_file()
            ),
            "temporary": git_dir.parent,
        }
        before = _object_store_snapshot(lender) if lender is not None else None
        output = await real_fetch(args, **kwargs)
        if lender is not None:
            record["lender_unchanged"] = _object_store_snapshot(lender) == before
        record["objects"], record["size_kib"] = _count_objects(git_dir)
        observed.append(record)
        return output

    monkeypatch.setattr(manager, "_arun_authenticated_git", watched_fetch)
    return observed


async def _exact_fetch(
    manager: GitManager, destination: Path | str, source: Path, oid: str, ref: str
) -> str:
    return await manager._afetch_exact_oid_with_app_auth_to_url(
        str(destination),
        destination_url=source.as_uri(),
        token="local-test-token",
        oid=oid,
        destination_ref=ref,
    )


@pytest.mark.asyncio
async def test_exact_oid_fetch_downloads_only_what_the_destination_lacks(tmp_path, monkeypatch):
    work, source, store = _exact_fetch_case(tmp_path)
    source_objects, _size = _count_objects(source)
    first_tip = _git(["rev-parse", "HEAD"], work)
    manager = GitManager()
    fetches = _watch_exact_fetches(manager, monkeypatch)

    await _exact_fetch(manager, store, source, first_tip, "refs/aq/exact/first")
    second_tip = _commit_and_publish(work, source, "one more\n")
    result = await _exact_fetch(manager, store, source, second_tip, "refs/aq/exact/second")

    first, second = fetches
    # An empty store has nothing to lend, so the first fetch is the whole history.
    assert first["seeded"] == []
    assert first["objects"] >= source_objects > 120
    assert first["size_kib"] >= 1024
    # The store holds only daemon refs (no origin remote): they are what the
    # second fetch reports as held, so one new commit is one commit, one tree
    # and one blob — not the 1 MiB history.
    assert second["seeded"] == ["refs/aq/have/0"]
    assert second["objects"] <= 5
    assert second["size_kib"] < 64
    assert result == second_tip
    assert _git(["for-each-ref", "--format=%(objectname) %(refname)"], store).splitlines() == [
        f"{first_tip} refs/aq/exact/first", f"{second_tip} refs/aq/exact/second"
    ]
    # The store owns every object it now references; nothing borrows back.
    assert not (store / "objects" / "info" / "alternates").exists()
    _git(["fsck", "--connectivity-only", "--no-dangling"], store)


@pytest.mark.asyncio
async def test_exact_oid_fetch_borrows_destination_objects_read_only(tmp_path, monkeypatch, caplog):
    work, source, store = _exact_fetch_case(tmp_path)
    manager = GitManager()
    first_tip = _git(["rev-parse", "HEAD"], work)
    await _exact_fetch(manager, store, source, first_tip, "refs/aq/exact/first")
    # Several refs naming one commit are reported to the remote once.
    _git(["update-ref", "refs/aq/exact/alias", first_tip], store)
    tip = _commit_and_publish(work, source, "one more\n")
    objects = store / "objects"
    fetches = _watch_exact_fetches(manager, monkeypatch, lender=objects)

    with caplog.at_level(logging.INFO, logger="src.git.manager"):
        await _exact_fetch(manager, store, source, tip, "refs/aq/exact/second")

    [observed] = fetches
    assert observed["alternates"] == f"{objects.resolve()}\n"
    assert observed["seeded"] == ["refs/aq/have/0"]
    # The credentialed process writes only into its own repository, and never
    # runs for-each-ref against the destination to discover its tips.
    assert observed["lender_unchanged"] is True
    assert "core.alternateRefsCommand=true" in observed["args"]
    assert "maintenance.auto=false" in observed["args"]
    assert not Path(observed["temporary"]).exists()
    assert not (objects / "info" / "alternates").exists()
    assert "exact_fetch=incremental, seeded_tips=1" in caplog.text
    assert "local-test-token" not in caplog.text
    # The seeded refs/aq/have/* names stay in the throwaway repository.
    assert _git(["for-each-ref", "--format=%(refname)"], store).split() == [
        "refs/aq/exact/alias", "refs/aq/exact/first", "refs/aq/exact/second"
    ]


@pytest.mark.asyncio
async def test_exact_oid_fetch_into_a_linked_worktree_borrows_the_common_object_store(
    tmp_path, monkeypatch
):
    work, source, _store = _exact_fetch_case(tmp_path)
    checkout = tmp_path / "checkout"
    _git(["clone", "--quiet", source.as_uri(), str(checkout)], tmp_path)
    slot = tmp_path / "slot"
    _git(["worktree", "add", "--quiet", "--detach", str(slot), "HEAD"], checkout)
    # The push path's observed-old-OID recovery passes the slot's own Git dir.
    git_dir = _git(["rev-parse", "--absolute-git-dir"], slot)
    assert Path(git_dir) != checkout / ".git"
    tip = _commit_and_publish(work, source, "one more\n")
    manager = GitManager()
    fetches = _watch_exact_fetches(manager, monkeypatch)

    await _exact_fetch(manager, git_dir, source, tip, "refs/aq/push-observed/test")

    [observed] = fetches
    assert observed["alternates"] == f"{(checkout / '.git' / 'objects').resolve()}\n"
    assert observed["objects"] <= 5
    assert _git(["rev-parse", "refs/aq/push-observed/test"], slot) == tip
    _git(["fsck", "--connectivity-only", "--no-dangling"], checkout)


@pytest.mark.asyncio
async def test_exact_oid_fetch_downloads_everything_for_a_partial_destination(
    tmp_path, monkeypatch, caplog
):
    # The PR-diff cache's existing-login branch fetches with --filter=blob:none,
    # so the cache can be a partial clone: its tips do not prove their blobs.
    work, source, store = _exact_fetch_case(tmp_path)
    _git(["config", "uploadpack.allowFilter", "true"], source)
    first_tip = _git(["rev-parse", "HEAD"], work)
    _git(["fetch", "--quiet", "--no-tags", "--filter=blob:none", source.as_uri(),
          f"{first_tip}:refs/aq/pr/head"], store)
    assert list((store / "objects" / "pack").glob("*.promisor"))
    tip = _commit_and_publish(work, source, "one more\n")
    manager = GitManager()
    fetches = _watch_exact_fetches(manager, monkeypatch)

    with caplog.at_level(logging.INFO, logger="src.git.manager"):
        assert await _exact_fetch(manager, store, source, tip, "refs/aq/pr/base") == tip

    [observed] = fetches
    assert (observed["alternates"], observed["seeded"]) == (None, [])
    assert observed["objects"] > 120
    assert "exact_fetch=full, seeded_tips=0" in caplog.text
    assert _git(["rev-parse", "refs/aq/pr/base"], store) == tip


@pytest.mark.asyncio
async def test_exact_oid_fetch_of_a_held_commit_pins_it_without_the_network(
    tmp_path, monkeypatch, caplog
):
    # parent CI imports a head for its trust manifest and then again to
    # publish it: the second import must not need (or fail on) the network.
    work, source, store = _exact_fetch_case(tmp_path)
    tip = _git(["rev-parse", "HEAD"], work)
    manager = GitManager()
    await _exact_fetch(manager, store, source, tip, "refs/aq/attestation-trust/x")

    async def no_network(*_args, **_kwargs):
        raise AssertionError("a held commit must not be fetched")

    monkeypatch.setattr(manager, "_arun_authenticated_git", no_network)
    with caplog.at_level(logging.INFO, logger="src.git.manager"):
        result = await _exact_fetch(manager, store, source, tip, "refs/aq/parent-ci/x")

    assert result == tip
    assert _git(["rev-parse", "refs/aq/parent-ci/x"], store) == tip
    assert "exact_fetch=local, seeded_tips=1, received_objects=0" in caplog.text


@pytest.mark.asyncio
async def test_exact_oid_fetch_completes_a_commit_held_without_its_history(
    tmp_path, monkeypatch
):
    work, source, store = _exact_fetch_case(tmp_path)
    tip = _git(["rev-parse", "HEAD"], work)
    # The commit object alone (say, left by an interrupted unpack) is not the
    # commit: its tree and parents are missing, so it must still be fetched.
    assert _write_object_only(work, store, tip) == tip
    manager = GitManager()
    fetches = _watch_exact_fetches(manager, monkeypatch)

    assert await _exact_fetch(manager, store, source, tip, "refs/aq/exact/tip") == tip

    assert len(fetches) == 1
    _git(["fsck", "--connectivity-only", "--no-dangling"], store)


@pytest.mark.asyncio
async def test_exact_oid_fetch_failure_names_its_mode(tmp_path, monkeypatch):
    work, source, store = _exact_fetch_case(tmp_path)
    manager = GitManager()

    async def timed_out(*_args, **_kwargs):
        raise GitError("authenticated Git acquisition failed: exception TimeoutError: phase=x")

    monkeypatch.setattr(manager, "_arun_authenticated_git", timed_out)
    with pytest.raises(GitError) as caught:
        await _exact_fetch(
            manager, store, source, _git(["rev-parse", "HEAD"], work), "refs/aq/exact/tip"
        )
    assert str(caught.value) == (
        "authenticated Git acquisition failed: exception TimeoutError: phase=x; "
        "exact_fetch=incremental, seeded_tips=0"
    )


@pytest.mark.asyncio
async def test_pr_delivery_diff_imports_pinned_oids_with_fresh_app_credentials(
    tmp_path, monkeypatch
):
    checkout, source, _trap, base, tip = _git_push_case(tmp_path)
    _git(["push", str(source), f"{tip}:refs/heads/topic"], checkout)
    access = _BoundAppAccess()
    manager = GitManager(github_access=access)
    real_fetch = manager._afetch_exact_oid_with_app_auth_to_url

    async def file_source_fetch(destination_git_dir, **kwargs):
        assert kwargs["destination_url"] == "https://github.com/acme/widgets.git"
        return await real_fetch(
            destination_git_dir, **(kwargs | {"destination_url": source.as_uri()})
        )

    monkeypatch.setattr(manager, "_afetch_exact_oid_with_app_auth_to_url", file_source_fetch)
    identity = PullRequestIdentity("acme/widgets", 12, "main", base, "topic", tip, 1)

    changed = await manager._apr_delivery_diff(
        str(tmp_path), identity, repository=GitHubRepositoryBinding(303, "acme/widgets")
    )

    assert changed == "file.txt\x00"
    assert access.requested == ["https://github.com/acme/widgets.git"]
    assert access.token_requests == [GitHubRepositoryBinding(303, "acme/widgets")] * 2


@pytest.mark.asyncio
async def test_app_fetch_rejects_origin_outside_authorized_repository_before_token_use(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(["init", "--initial-branch=main"], checkout)
    _git(["remote", "add", "origin", "https://github.com/other/repository.git"], checkout)
    access = _BoundAppAccess()
    manager = GitManager(github_access=access)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("network fetch ran for a foreign checkout remote")

    monkeypatch.setattr(manager, "_afetch_origin_with_auth_to_url", forbidden)
    with pytest.raises(GitError, match="did not match"):
        await manager.afetch_origin(
            str(checkout), repository_url="git@github.com:acme/widgets.git"
        )
    assert access.token_requests == []


@pytest.mark.asyncio
async def test_app_fetch_requires_explicit_authority_for_github_remote(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(["init", "--initial-branch=main"], checkout)
    _git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], checkout)
    access = _BoundAppAccess()

    with pytest.raises(GitError, match="authorized GitHub repository is required"):
        await GitManager(github_access=access).afetch_origin(
            str(checkout), repository_url=""
        )
    assert access.requested == []
    assert access.token_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pull", "merge", "default_branch"])
async def test_legacy_git_operations_refuse_unbound_app_origin(
    tmp_path, monkeypatch, operation
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(["init", "--initial-branch=main"], checkout)
    _git(["config", "user.name", "Test"], checkout)
    _git(["config", "user.email", "test@example.com"], checkout)
    (checkout / "file.txt").write_text("base")
    _git(["add", "file.txt"], checkout)
    _git(["commit", "-m", "base"], checkout)
    _git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], checkout)
    access = _BoundAppAccess()
    manager = GitManager(github_access=access)
    helper_called = tmp_path / "operator-helper-called"
    helper = tmp_path / "operator-helper"
    helper.write_text(f"#!/bin/sh\ntouch {helper_called}\n")
    helper.chmod(0o700)
    global_config = tmp_path / "operator-gitconfig"
    global_config.write_text(f"[credential]\n\thelper = !{helper}\n")
    monkeypatch.setitem(manager._SUBPROCESS_ENV, "GIT_CONFIG_GLOBAL", str(global_config))
    original_arun = manager._arun

    async def reject_ambient_network(args, **kwargs):
        if args[0] in {"fetch", "pull", "push", "ls-remote"}:
            raise AssertionError("legacy network Git bypassed App authentication")
        return await original_arun(args, **kwargs)

    monkeypatch.setattr(manager, "_arun", reject_ambient_network)
    if operation == "pull":
        call = manager.apull_branch(str(checkout), "main")
    elif operation == "merge":
        call = manager.amerge_branch(str(checkout), "main")
    else:
        _git(["branch", "-m", "topic"], checkout)
        call = manager.aget_default_branch(str(checkout))
    with pytest.raises(GitError, match="authorized GitHub repository is required"):
        await call
    assert not helper_called.exists()
    assert access.requested == []
    assert access.token_requests == []


@pytest.mark.asyncio
async def test_app_pull_fetches_authorized_origin_then_merges_local_ref(tmp_path, monkeypatch):
    checkout, _target, _trap, base, tip = _git_push_case(tmp_path)
    _git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], checkout)
    _git(["update-ref", "refs/remotes/origin/main", tip], checkout)
    _git(["reset", "--hard", base], checkout)
    manager = GitManager(github_access=_BoundAppAccess())
    fetch = AsyncMock()
    monkeypatch.setattr(manager, "afetch_origin", fetch)
    assert await manager.apull_branch(
        str(checkout), "main", repository_url="https://github.com/acme/widgets.git"
    ) == "main"
    fetch.assert_awaited_once_with(
        str(checkout), repository_url="https://github.com/acme/widgets.git"
    )
    assert _git(["rev-parse", "HEAD"], checkout) == tip


@pytest.mark.asyncio
async def test_app_merge_fetches_authorized_origin_before_reset(tmp_path, monkeypatch):
    checkout, _target, _trap, base, tip = _git_push_case(tmp_path)
    _git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], checkout)
    _git(["branch", "feature", tip], checkout)
    _git(["update-ref", "refs/remotes/origin/main", base], checkout)
    manager = GitManager(github_access=_BoundAppAccess())
    fetch = AsyncMock()
    monkeypatch.setattr(manager, "afetch_origin", fetch)
    assert await manager.amerge_branch(
        str(checkout), "feature", repository_url="https://github.com/acme/widgets.git"
    )
    fetch.assert_awaited_once_with(
        str(checkout), repository_url="https://github.com/acme/widgets.git"
    )
    assert _git(["rev-parse", "HEAD"], checkout) == tip


@pytest.mark.asyncio
async def test_authenticated_default_branch_discovery_uses_pinned_https_source(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(["init", "--initial-branch=main"], checkout)
    _git(["remote", "add", "origin", "git@github.com:acme/widgets.git"], checkout)
    manager = GitManager(github_access=_BoundAppAccess())
    captured = {}

    async def ls_remote(args, **kwargs):
        captured.update(args=args, **kwargs)
        return b"ref: refs/heads/trunk\tHEAD\n1234567890abcdef1234567890abcdef12345678\tHEAD\n"

    monkeypatch.setattr(manager, "_arun_authenticated_git", ls_remote)
    assert await manager.aget_default_branch(
        str(checkout), repository_url="git@github.com:acme/widgets.git"
    ) == "trunk"
    assert captured["repository_url"] == "https://github.com/acme/widgets.git"
    assert captured["token"] == "app-token"


@pytest.mark.asyncio
async def test_authenticated_fetch_keeps_shared_repository_lock(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(["init", "--initial-branch=main"], checkout)
    _git(["remote", "add", "origin", "https://github.com/acme/widgets.git"], checkout)
    manager = GitManager(github_access=_BoundAppAccess())
    lock = asyncio.Lock()
    manager.set_lock_provider(lambda _cwd: lock)
    entered = asyncio.Event()
    release = asyncio.Event()
    runs = 0

    async def held_fetch(*_args, **_kwargs):
        nonlocal runs
        runs += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr(manager, "_afetch_origin_with_auth_to_url", held_fetch)
    first = asyncio.create_task(
        manager.afetch_origin(str(checkout), repository_url="https://github.com/acme/widgets.git")
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    second = asyncio.create_task(
        manager.afetch_origin(str(checkout), repository_url="https://github.com/acme/widgets.git")
    )
    await asyncio.sleep(0.05)
    assert runs == 1
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert runs == 2


@pytest.mark.asyncio
async def test_worktree_base_fetch_passes_project_repository_to_authenticated_git(monkeypatch):
    class DB:
        async def get_project(self, project_id):
            assert project_id == "project-1"
            return SimpleNamespace(repo_url="git@github.com:acme/widgets.git")

    git = SimpleNamespace(ahas_remote=AsyncMock(return_value=True), afetch_origin=AsyncMock())
    slots = WorktreeSlotManager(DB(), git, None, WorktreesConfig(), lambda _path: asyncio.Lock())
    monkeypatch.setattr(slots, "_ref_exists", AsyncMock(return_value=True))

    assert await slots._fetch_and_resolve_start_ref(
        "/checkout", "main", required=True, project_id="project-1"
    ) == "origin/main"
    git.afetch_origin.assert_awaited_once_with(
        "/checkout", repository_url="git@github.com:acme/widgets.git", lock_held=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "cancellation"])
async def test_authenticated_acquisition_timeout_or_cancellation_reaps_process_group(
    tmp_path, cancel
):
    pids = tmp_path / "acquisition-pids"
    hanging_git = tmp_path / "hanging-git"
    hanging_git.write_text(
        "#!/bin/sh\n"
        "sleep 300 &\n"
        "child=$!\n"
        f'printf \'%s %s\' "$$" "$child" > {pids}\n'
        'wait "$child"\n'
    )
    hanging_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(hanging_git)
    home = tmp_path / "home"
    home.mkdir()
    deadline = asyncio.get_running_loop().time() + (1.0 if not cancel else 30.0)
    task = asyncio.create_task(
        manager._arun_authenticated_git(
            ["ls-remote", (tmp_path / "source.git").as_uri(), "HEAD"],
            home=home,
            repository_url=(tmp_path / "source.git").as_uri(),
            token="test-token",
            deadline=deadline,
        )
    )
    recorded = await _wait_for_file(pids, fields=2)
    leader, child = map(int, recorded.split())
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(GitError, match="acquisition failed") as caught:
            await task
        message = str(caught.value)
        assert "phase=git_communicate" in message
        assert "configured_budget=1.0s" in message
        assert "outer_deadline_expired=True" in message
        assert "broker_state=" in message
    assert not _process_group_exists(leader)
    assert not Path(f"/proc/{child}").exists()


@pytest.mark.asyncio
async def test_authenticated_acquisition_expired_budget_identifies_preflight(tmp_path, monkeypatch):
    manager = GitManager()
    home = tmp_path / "home"
    home.mkdir()

    async def slow_topology(**_kwargs):
        await asyncio.sleep(0.02)
        return object()

    monkeypatch.setattr(manager, "_app_git_credential_topology", slow_topology)
    with pytest.raises(GitError, match="acquisition failed") as caught:
        await manager._arun_authenticated_git(
            ["ls-remote", (tmp_path / "source.git").as_uri(), "HEAD"],
            home=home,
            repository_url=(tmp_path / "source.git").as_uri(),
            token="test-token",
            deadline=asyncio.get_running_loop().time() + 0.005,
            budget_seconds=0.005,
        )
    message = str(caught.value)
    assert "phase=budget_preflight" in message
    assert "outer_deadline_expired=False" in message
    assert "broker_state=not_started" in message


@pytest.mark.asyncio
async def test_authenticated_acquisition_retries_timeout_with_same_delivery_oid_and_lease(
    tmp_path, monkeypatch
):
    manager = GitManager()
    source = (tmp_path / "source.git").as_uri()
    old_oid, tip_oid = "a" * 40, "b" * 40
    started = []
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(f"{old_oid}\trefs/heads/main\n".encode(), b"")),
    )

    async def start(*args, **kwargs):
        started.append((args, kwargs))
        if len(started) == 1:
            raise asyncio.TimeoutError
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(manager_module.random, "uniform", lambda low, high: 0.2)
    monkeypatch.setattr(manager, "_kill_app_git_group", AsyncMock())
    monkeypatch.setattr(manager, "_apush_destination", AsyncMock(return_value=(source, None)))
    monkeypatch.setattr(manager, "_aresolve_delivery_tip", AsyncMock(return_value=tip_oid))
    monkeypatch.setattr(manager, "areserved_paths_in_diff", AsyncMock(return_value=[]))
    transfer = AsyncMock()
    monkeypatch.setattr(manager, "_atransfer_exact_ref", transfer)
    monkeypatch.setattr(manager, "_aemit_push_event", AsyncMock())

    result = await manager.apush_validated_delivery(
        str(tmp_path), "main", "refs/heads/source", "main",
        expected_remote_oid=old_oid, repository_url=source,
    )

    assert result == tip_oid
    assert len(started) == 2
    assert started[0][0] == started[1][0]
    assert started[0][1]["env"] == started[1][1]["env"]
    assert transfer.await_args.args[1] == tip_oid
    assert transfer.await_args.args[3] == old_oid


@pytest.mark.asyncio
async def test_authenticated_acquisition_timeout_stops_after_one_retry(tmp_path, monkeypatch):
    manager = GitManager()
    home = tmp_path / "home"
    home.mkdir()
    source = (tmp_path / "source.git").as_uri()
    starts = 0
    attempt_deadlines = []
    original_attempt = manager._arun_authenticated_git_once

    async def record_attempt(*args, **kwargs):
        attempt_deadlines.append(kwargs["deadline"])
        return await original_attempt(*args, **kwargs)

    async def time_out(*_args, **_kwargs):
        nonlocal starts
        starts += 1
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", time_out)
    monkeypatch.setattr(manager_module.random, "uniform", lambda low, high: 0.2)
    monkeypatch.setattr(manager, "_arun_authenticated_git_once", record_attempt)
    overall_deadline = asyncio.get_running_loop().time() + 9
    with pytest.raises(GitError, match="exception TimeoutError") as caught:
        await manager._arun_authenticated_git(
            ["ls-remote", source, "HEAD"], home=home, repository_url=source,
            token=None, deadline=overall_deadline,
            budget_seconds=9,
        )
    assert starts == 2
    assert attempt_deadlines[0] < attempt_deadlines[1] == overall_deadline
    assert type(caught.value) is GitError
    message = str(caught.value)
    assert "attempt=2" in message
    assert "phase=subprocess_spawn" in message
    assert "configured_budget=9.0s" in message
    assert "elapsed_in_run=" in message


@pytest.mark.asyncio
async def test_authenticated_acquisition_retries_stalled_https_with_budget_left(
    tmp_path, monkeypatch
):
    _checkout, source, _trap, oid, _tip = _git_push_case(tmp_path)
    advertisement = subprocess.run(
        ["git", "upload-pack", "--stateless-rpc", "--advertise-refs", str(source)],
        check=True, capture_output=True,
    ).stdout
    advertisement = b"001e# service=git-upload-pack\n0000" + advertisement
    requests = 0

    async def respond(reader, writer):
        nonlocal requests
        try:
            await reader.readuntil(b"\r\n\r\n")
            requests += 1
            if requests == 1:
                # Accept TLS and the request, but never send an HTTP response.
                await reader.read()
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/x-git-upload-pack-advertisement\r\n"
                    + f"Content-Length: {len(advertisement)}\r\nConnection: close\r\n\r\n".encode()
                    + advertisement
                )
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(
        respond, "127.0.0.1", 0, ssl=_local_tls_context(tmp_path)
    )
    port = server.sockets[0].getsockname()[1]
    source_url = f"https://127.0.0.1:{port}/source.git"
    manager = GitManager()
    monkeypatch.setattr(manager, "_APP_GIT_LOW_SPEED_TIME", 2)
    monkeypatch.setitem(manager._SUBPROCESS_ENV, "GIT_HTTP_LOW_SPEED_LIMIT", "0")
    monkeypatch.setitem(manager._SUBPROCESS_ENV, "GIT_HTTP_LOW_SPEED_TIME", "0")
    monkeypatch.setattr(manager_module.random, "uniform", lambda _low, _high: 0.2)
    original_attempt = manager._arun_authenticated_git_once
    attempts = []
    low_speed_failure = []
    loop = asyncio.get_running_loop()
    overall_deadline = loop.time() + 12

    async def record_attempt(*args, **kwargs):
        attempts.append((kwargs["attempt"], overall_deadline - loop.time()))
        try:
            return await original_attempt(*args, **kwargs)
        except manager_module._AuthenticatedGitTimeout as exc:
            low_speed_failure.append(str(exc))
            raise

    monkeypatch.setattr(manager, "_arun_authenticated_git_once", record_attempt)
    try:
        output = await manager._arun_authenticated_git(
            ["-c", "http.sslVerify=false", "-c", "protocol.version=0", "ls-remote",
             source_url, "HEAD"],
            home=tmp_path,
            repository_url=source.as_uri(),  # local test seam; transport is real HTTPS
            token=None,
            deadline=overall_deadline,
            budget_seconds=12,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert output == f"{oid}\tHEAD\n".encode()
    assert requests == 2
    assert [attempt for attempt, _remaining in attempts] == [1, 2]
    assert attempts[1][1] > 8  # the retry keeps most of the original 12 seconds
    assert len(low_speed_failure) == 1
    assert "low-speed abort" in low_speed_failure[0]
    assert "phase=git_communicate, attempt=1" in low_speed_failure[0]
    assert "outer_deadline_expired=False" in low_speed_failure[0]


@pytest.mark.asyncio
async def test_authenticated_acquisition_allows_slow_progressing_https(tmp_path, monkeypatch):
    _checkout, source, _trap, oid, _tip = _git_push_case(tmp_path)
    advertisement = subprocess.run(
        ["git", "upload-pack", "--stateless-rpc", "--advertise-refs", str(source)],
        check=True, capture_output=True,
    ).stdout
    advertisement = b"001e# service=git-upload-pack\n0000" + advertisement
    chunks = [advertisement[i:i + 8] for i in range(0, len(advertisement), 8)]
    requests = 0

    async def respond(reader, writer):
        nonlocal requests
        try:
            await reader.readuntil(b"\r\n\r\n")
            requests += 1
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/x-git-upload-pack-advertisement\r\n"
                + f"Content-Length: {len(advertisement)}\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
            for chunk in chunks:
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(2.5 / len(chunks))
        finally:
            writer.close()

    server = await asyncio.start_server(
        respond, "127.0.0.1", 0, ssl=_local_tls_context(tmp_path)
    )
    port = server.sockets[0].getsockname()[1]
    source_url = f"https://127.0.0.1:{port}/source.git"
    manager = GitManager()
    monkeypatch.setattr(manager, "_APP_GIT_LOW_SPEED_TIME", 2)
    start = asyncio.get_running_loop().time()
    try:
        output = await manager._arun_authenticated_git(
            ["-c", "http.sslVerify=false", "-c", "protocol.version=0", "ls-remote",
             source_url, "HEAD"],
            home=tmp_path,
            repository_url=source.as_uri(),
            token=None,
            deadline=start + 10,
            budget_seconds=10,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert output == f"{oid}\tHEAD\n".encode()
    assert asyncio.get_running_loop().time() - start > 2
    assert requests == 1


@pytest.mark.asyncio
async def test_authenticated_acquisition_does_not_retry_auth_or_nonzero_exit(
    tmp_path, monkeypatch
):
    manager = GitManager()
    home = tmp_path / "home"
    home.mkdir()
    source = (tmp_path / "source.git").as_uri()
    starts = 0
    process = SimpleNamespace(returncode=7, communicate=AsyncMock(return_value=(b"", b"auth failed")))

    async def start(*_args, **_kwargs):
        nonlocal starts
        starts += 1
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    monkeypatch.setattr(manager, "_kill_app_git_group", AsyncMock())
    with pytest.raises(GitError, match="returncode 7"):
        await manager._arun_authenticated_git(
            ["ls-remote", source, "HEAD"], home=home, repository_url=source,
            token=None, deadline=asyncio.get_running_loop().time() + 5,
        )
    assert starts == 1
    with pytest.raises(GitError, match="invalid GitHub App credential"):
        await manager._arun_authenticated_git(
            ["ls-remote", source, "HEAD"], home=home, repository_url=source,
            token="", deadline=asyncio.get_running_loop().time() + 5,
        )
    assert starts == 1


@pytest.mark.asyncio
async def test_authenticated_git_failure_reports_exit_and_scrubs_stderr_and_publisher_log(
    tmp_path, caplog
):
    token = "private-installation-token"
    fake_git = tmp_path / "failing-git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' 'fatal: unable to access "
        "https://alice:password@github.com/acme/widgets.git' "
        "'Authorization: Bearer private-installation-token' "
        "'token=private-installation-token' 'fatal: upstream unavailable' >&2\n"
        "exit 7\n"
    )
    fake_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(fake_git)
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(GitError) as caught:
        await manager._arun_authenticated_git(
            ["ls-remote", (tmp_path / "source.git").as_uri(), "HEAD"],
            home=home,
            repository_url=(tmp_path / "source.git").as_uri(),
            token=token,
            deadline=asyncio.get_running_loop().time() + 5,
        )

    message = str(caught.value)
    assert "returncode 7" in message
    assert "fatal: upstream unavailable" in message
    publisher = DevelopmentIntegration(None, data_dir=tmp_path, git=manager)
    with caplog.at_level(logging.WARNING, logger="src.integration.development"):
        publisher._note_project_fault("agent-queue", caught.value)
    for output in (message, caplog.text):
        assert token not in output
        assert "password" not in output
        assert "alice:" not in output
        assert "Authorization: Bearer" not in output
        assert "https://alice:password@" not in output


@pytest.mark.asyncio
async def test_authenticated_git_success_without_credential_request_is_not_a_failure(tmp_path):
    """A public repository answers anonymously, so Git never asks for the token."""
    fake_git = tmp_path / "anonymous-git"
    fake_git.write_text("#!/bin/sh\necho anonymous-output\nexit 0\n")
    fake_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(fake_git)
    home = tmp_path / "home"
    home.mkdir()

    output = await manager._arun_authenticated_git(
        ["ls-remote", "https://github.com/acme/widgets.git", "HEAD"],
        home=home,
        repository_url="https://github.com/acme/widgets.git",
        token="private-installation-token",
        deadline=asyncio.get_running_loop().time() + 5,
    )

    assert output == b"anonymous-output\n"
    _assert_no_broker_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requests_credential", "exit_code"), [(True, 0), (True, 128), (False, 128)]
)
async def test_authenticated_git_failure_reports_broker_not_served(
    tmp_path, requests_credential, exit_code
):
    # The askpass request reaches the broker, which refuses it: the requester
    # is not the pinned remote helper under Git.
    prompt = (
        "\"$GIT_ASKPASS\" \"Password for 'https://x-access-token@github.com': \" >/dev/null\n"
        if requests_credential else ""
    )
    fake_git = tmp_path / "prompting-git"
    fake_git.write_text(f"#!/bin/sh\n{prompt}exit {exit_code}\n")
    fake_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(fake_git)
    # Settled when Git exits; long enough that a loaded host still sees the request.
    manager._APP_CREDENTIAL_BROKER_TIMEOUT = 5.0
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(GitError) as caught:
        await manager._arun_authenticated_git(
            ["ls-remote", "https://github.com/acme/widgets.git", "HEAD"],
            home=home,
            repository_url="https://github.com/acme/widgets.git",
            token="private-installation-token",
            deadline=asyncio.get_running_loop().time() + 10,
        )

    message = str(caught.value)
    assert "credential broker did not serve token" in message
    assert f"credential_requested={requests_credential}" in message
    assert ("returncode 128" in message) is bool(exit_code)
    assert "timeout=5.0s" in message
    assert "budget_at_start=" in message
    assert "remaining_push_budget=" in message
    _assert_no_broker_tasks()


@pytest.mark.asyncio
async def test_app_origin_fetch_of_public_repository_succeeds_without_credential_request(
    tmp_path, monkeypatch
):
    _work, _source, destination, tip = _github_stand_in_case(tmp_path)
    manager = GitManager()

    async with _GitHubStandIn(tmp_path, tmp_path / "github", required_token=None) as stand_in:
        _route_app_git_through(manager, stand_in, monkeypatch)
        await manager._afetch_origin_with_auth_to_url(
            str(destination),
            source_url="https://github.com/acme/widgets.git",
            token="app-installation-token",
        )

    assert _git(["rev-parse", "refs/remotes/origin/topic"], destination) == tip
    assert _git(["rev-parse", "refs/tags/archive/topic"], destination) == tip
    assert stand_in.tunnels
    assert all(tunnel.startswith("CONNECT github.com:443 ") for tunnel in stand_in.tunnels)
    assert stand_in.authorizations and set(stand_in.authorizations) == {None}


@pytest.mark.asyncio
async def test_app_git_takes_credential_only_from_broker_despite_competing_helpers(
    tmp_path, monkeypatch
):
    _work, _source, destination, tip = _github_stand_in_case(tmp_path)
    invoked = tmp_path / "competing-credential-invoked"
    helper = tmp_path / "competing-helper"
    helper.write_text(
        "#!/bin/sh\n"
        f'echo "$0 $*" >> {invoked}\n'
        "printf 'username=x-access-token\\npassword=operator-login-token\\n'\n"
    )
    helper.chmod(0o700)
    competing = (
        f"[credential]\n\thelper = {helper}\n"
        f'[credential "https://github.com"]\n\thelper =\n\thelper = {helper}\n'
    )
    operator_home = tmp_path / "operator-home"
    (operator_home / ".config" / "git").mkdir(parents=True)
    (operator_home / ".gitconfig").write_text(competing)
    (operator_home / ".config" / "git" / "config").write_text(competing)
    netrc = operator_home / ".netrc"
    netrc.write_text("machine github.com login x-access-token password operator-netrc-token\n")
    netrc.chmod(0o600)
    operator_global = tmp_path / "operator-global-config"
    operator_global.write_text(competing)
    manager = GitManager()
    # The daemon's own environment, as the operator's `gh auth setup-git` leaves it.
    for name, value in {
        "HOME": str(operator_home),
        "XDG_CONFIG_HOME": str(operator_home / ".config"),
        "GIT_CONFIG_GLOBAL": str(operator_global),
        "GIT_CONFIG_SYSTEM": str(operator_global),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": str(helper),
        "GIT_CONFIG_PARAMETERS": f"'credential.helper'='{helper}'",
        "GIT_ASKPASS": str(helper),
        "SSH_ASKPASS": str(helper),
        "GH_TOKEN": "operator-gh-token",
    }.items():
        monkeypatch.setenv(name, value)
        monkeypatch.setitem(manager._SUBPROCESS_ENV, name, value)

    async with _GitHubStandIn(
        tmp_path, tmp_path / "github", required_token="app-installation-token"
    ) as stand_in:
        _route_app_git_through(manager, stand_in, monkeypatch)
        await manager._afetch_origin_with_auth_to_url(
            str(destination),
            source_url="https://github.com/acme/widgets.git",
            token="app-installation-token",
        )

    assert _git(["rev-parse", "refs/remotes/origin/topic"], destination) == tip
    presented = {
        base64.b64decode(authorization.removeprefix("Basic ")).decode()
        for authorization in stand_in.authorizations
        if authorization is not None
    }
    # Only the broker holds the App token, so presenting it proves the broker
    # served.  curl's own retry of the challenge carries the URL username with
    # an empty password; no other secret may ever reach the remote.
    assert "x-access-token:app-installation-token" in presented
    assert presented <= {"x-access-token:", "x-access-token:app-installation-token"}
    assert not invoked.exists()


@pytest.mark.asyncio
async def test_authenticated_git_failure_reports_sanitized_exception(tmp_path, monkeypatch):
    token = "private-installation-token"
    manager = GitManager()
    home = tmp_path / "home"
    home.mkdir()

    async def fail_to_launch(*_args, **_kwargs):
        raise RuntimeError(
            "launch failed for https://alice:password@github.com/acme/widgets.git " + token
        )

    monkeypatch.setattr(manager, "_app_git_credential_topology", AsyncMock(return_value=object()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_to_launch)
    with pytest.raises(GitError) as caught:
        await manager._arun_authenticated_git(
            ["ls-remote", (tmp_path / "source.git").as_uri(), "HEAD"],
            home=home,
            repository_url=(tmp_path / "source.git").as_uri(),
            token=token,
            deadline=asyncio.get_running_loop().time() + 5,
        )

    message = str(caught.value)
    assert "exception RuntimeError: launch failed" in message
    assert token not in message
    assert "alice:password" not in message


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


async def _wait_for_file(path: Path, *, fields: int = 0) -> str:
    """Wait for ``path`` and return its text once it holds ``fields`` words.

    Existence alone is not enough: a shell redirect creates the file before the
    writer has put anything in it, so a loaded runner can read it empty.  The
    caller says how many whitespace-separated values it needs.
    """
    for _ in range(1000):
        try:
            text = path.read_text()
        except FileNotFoundError:
            text = ""
        if text and len(text.split()) >= fields:
            return text
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {fields} value(s) in {path}")


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
                    remote_name="https://github.com/acme/widgets.git",
                    remote_url="https://github.com/acme/widgets.git",
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
        f'printf \'%s %s\' "$$" "$child" > {pid_file}.tmp\n'
        f"mv {pid_file}.tmp {pid_file}\n"
        'wait "$child"\n'
    )
    hanging_git.chmod(0o700)
    manager = GitManager()
    manager._APP_GIT_EXECUTABLE = str(hanging_git)
    # Long enough that a loaded runner still records the pids before the kill
    # lands — the assertion is that the group dies, not how fast.
    timeout = 2.0 if not cancel else 30
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
    recorded = await _wait_for_file(pid_file, fields=2)
    leader, descendant = (int(value) for value in recorded.split())
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
        # The retainer is orphaned once the leader exits; the kill path can
        # only reap the leader, so init reaps the SIGKILLed retainer's zombie
        # asynchronously and the group can linger for a moment.
        for _ in range(200):
            if not _process_group_exists(leader):
                break
            await asyncio.sleep(0.01)
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
@pytest.mark.parametrize(
    "broker_url",
    ["transport", "original", "other_repository", "wrong_alias"],
)
@pytest.mark.parametrize("operation", ["ls-remote", "clone"])
async def test_git_app_broker_refuses_other_repository_and_mismatched_remote_helpers(
    tmp_path, broker_url, operation
):
    requests = 0
    first_http_request = asyncio.Event()

    async def respond(reader, writer):
        nonlocal requests
        requests += 1
        await reader.readuntil(b"\r\n\r\n")
        first_http_request.set()
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

    server = await asyncio.start_server(
        respond, "127.0.0.1", 0, ssl=_local_tls_context(tmp_path)
    )
    port = server.sockets[0].getsockname()[1]
    repository = f"https://127.0.0.1:{port}/acme/widgets.git"
    remote_url = repository.replace("https://", "https://x-access-token@", 1)
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
    git_args = (
        ["ls-remote", remote_url]
        if operation == "ls-remote"
        else ["clone", "--bare", remote_url, str(tmp_path / "clone.git")]
    )
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/git",
        "-c",
        "http.sslVerify=false",
        *git_args,
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
        # Wait until Git has contacted the repository before timing the broker.
        # Clone startup can otherwise outlast a short refusal timeout on a busy host.
        await asyncio.wait_for(first_http_request.wait(), timeout=10)
        served = await asyncio.wait_for(
            serve_one_credential(
                broker,
                token,
                git_pid=process.pid,
                topology=topology,
                authority=authority,
                repository=repository,
                remote_name=(
                    "wrong-alias"
                    if broker_url == "wrong_alias"
                    else "origin" if operation == "clone" else remote_url
                ),
                remote_url={
                    "transport": remote_url,
                    "original": repository,
                    "other_repository": remote_url.replace("widgets", "other"),
                    "wrong_alias": remote_url,
                }[broker_url],
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
    if broker_url == "other_repository":
        assert served is False, "App credentials must be refused for a different repository"
    else:
        assert served is (broker_url == "transport"), (requests, stderr)
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
