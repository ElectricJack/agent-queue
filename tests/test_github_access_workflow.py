"""Process-backed, private-repository workflow contract for both auth modes.

The executable below is a deterministic gh endpoint.  No GitHub account, daemon
database, operator login, or network repository is used by this suite.  The
separate disposable-repository run is described in the acceptance runbook.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.config import AppConfig, DatabaseConfig, DiscordConfig, GitHubAppConfig, ProjectRoot
from src.database import Database
from src.git.github import GitHubAccess, GitHubClient
from src.git.github_app import AppTokenCandidate
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubRepositoryBinding,
)
from src.git.manager import GitManager
from src.models import (
    Agent, AgentProfile, AgentState, Project, RepoSourceType, SessionRecord,
    Task, TaskStatus, Workspace,
)
from tests.db_fixtures import lease_dsn


HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
REPOSITORY = GitHubRepositoryBinding(303, "acme/widgets")
OTHER = GitHubRepositoryBinding(404, "acme/gadgets")
PR_URL = "https://github.com/acme/widgets/pull/7"

# Every response and state transition is produced by a child process.  The
# event file records only a credential *kind* and an installation generation,
# never a token value.  This also makes accidentally leaking a secret in a
# failed assertion less likely.
FAKE_GH = r'''#!/usr/bin/python3
import json, os, pathlib, sys, urllib.parse

state_path = pathlib.Path(os.environ["AQ_FAKE_GH_STATE"])
log_path = pathlib.Path(os.environ["AQ_FAKE_GH_LOG"])
state = json.loads(state_path.read_text())
argv = sys.argv[1:]
token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
kind = "app" if token and token.startswith("app-") else "login" if token or os.environ.get("AQ_FAKE_STORED_LOGIN") else "none"
generation = token.rsplit("-", 1)[-1] if kind == "app" else None
repo = argv[argv.index("--repo") + 1] if "--repo" in argv else None
event = {"argv": argv, "kind": kind, "generation": generation, "repo": repo,
         "config_isolated": os.environ.get("GH_CONFIG_DIR") != state["operator_config"],
         "has_secondary_token": "GITHUB_TOKEN" in os.environ}
with log_path.open("a") as log:
    log.write(json.dumps(event) + "\n")

if kind == "none" or (kind == "app" and state.get("deny_app")):
    if argv[0] != "api":
        sys.stderr.write("permission denied\n")
        sys.exit(4)
    status, payload = 403, {"message": "Resource not accessible by integration"}
else:
    status, payload = 200, None
    if argv[0] == "api":
        endpoint = argv[argv.index("--input") - 1] if "--input" in argv else argv[-1]
        method = argv[argv.index("--method") + 1]
        if endpoint.startswith("repos/") and len(endpoint.split("/")) == 3:
            name = endpoint.removeprefix("repos/")
            payload = {"id": 303 if name == "acme/widgets" else 404, "full_name": name}
        elif "/git/ref/heads/" in endpoint:
            branch = urllib.parse.unquote(endpoint.split("/git/ref/heads/", 1)[1])
            oid = state["heads"].get(branch)
            if oid is None:
                status, payload = 404, {"message": "Not Found"}
            else:
                payload = {"ref": "refs/heads/" + branch, "object": {"sha": oid}}
        elif "/pulls?" in endpoint:
            payload = [pull for pull in (state.get("pull"), state.get("audit_pull")) if pull]
            if "head=" in endpoint:
                requested = urllib.parse.unquote(endpoint.split("head=", 1)[1].split("&", 1)[0])
                payload = [pull for pull in payload if "acme:" + pull["head"]["ref"] == requested]
        elif endpoint.endswith("/pulls") and method == "POST":
            body = json.load(sys.stdin)
            branch = body["head"]
            payload = {
                "number": 8, "html_url": "https://github.com/acme/widgets/pull/8",
                "state": "open", "body": body["body"],
                "head": {"ref": branch, "sha": state["heads"][branch],
                         "repo": {"id": 303, "full_name": "acme/widgets"}},
                "base": {"ref": body["base"],
                         "repo": {"id": 303, "full_name": "acme/widgets"}},
            }
            state["audit_pull"] = payload
            state_path.write_text(json.dumps(state))
            status = 201
        elif endpoint.endswith("/pulls/7"):
            payload = state["pull"]
        elif "/commits/" in endpoint and endpoint.endswith("/check-runs?per_page=100"):
            payload = {"check_runs": [{"name": "required", "conclusion": "success"}]}
        elif "/commits/" in endpoint:
            payload = {"sha": state["heads"]["main"]}
        elif "/compare/" in endpoint:
            payload = {"behind_by": 0, "status": "ahead"}
        elif "/actions/jobs/" in endpoint and endpoint.endswith("/logs"):
            payload = "required: pass\n"
        else:
            status, payload = 404, {"message": "Not Found"}
    elif argv[:2] == ["pr", "create"]:
        assert repo == "acme/widgets"
        assert argv[argv.index("--head") + 1] == "aq/task"
        assert argv[argv.index("--base") + 1] == "main"
        assert argv[argv.index("--body-file") + 1] == "-"
        assert sys.stdin.read() == "private fixture delivery"
        state["pull"] = {
            "number": 7, "html_url": "https://github.com/acme/widgets/pull/7",
            "state": "open", "merged_at": None, "merge_commit_sha": None,
            "head": {"ref": "aq/task", "sha": state["heads"]["aq/task"],
                     "repo": {"id": 303, "full_name": "acme/widgets"}},
            "base": {"ref": "main", "repo": {"id": 303, "full_name": "acme/widgets"}},
        }
        state_path.write_text(json.dumps(state))
        sys.stdout.write(state["pull"]["html_url"] + "\n")
        sys.exit(0)
    elif argv[:2] == ["pr", "view"]:
        payload = {"statusCheckRollup": [{"name": "required", "conclusion": "SUCCESS"}]}
    elif argv[:2] == ["pr", "merge"]:
        assert argv[argv.index("--match-head-commit") + 1] == state["heads"]["aq/task"]
        state["pull"].update(state="closed", merged_at="2026-09-22T00:00:00Z",
                             merge_commit_sha="c" * 40)
        state["heads"].pop("aq/task")
        state["heads"]["main"] = "c" * 40
        state_path.write_text(json.dumps(state))
        sys.stdout.write("Merged pull request #7 (" + "c" * 40 + ").\n")
        sys.exit(0)
    else:
        sys.stderr.write("unsupported fake gh command\n")
        sys.exit(2)

if argv[0] == "api" and "--include" in argv:
    reason = "Created" if status == 201 else "OK" if status == 200 else "Forbidden" if status == 403 else "Not Found"
    sys.stdout.write(f"HTTP/2.0 {status} {reason}\r\n\r\n")
if isinstance(payload, str):
    sys.stdout.write(payload)
else:
    sys.stdout.write(json.dumps(payload) + "\n")
sys.exit(0 if status in (200, 201) else 1)
'''


@dataclass
class InstallationProvider:
    now: list[float]
    calls: list[GitHubRepositoryBinding] = field(default_factory=list)
    fail: bool = False

    credential_identity = GitHubCredentialIdentity.app(101, 202)

    async def mint(self, repository: GitHubRepositoryBinding) -> AppTokenCandidate:
        self.calls.append(repository)
        if self.fail:
            raise GitHubAccessError("credentials", "installation permission denied")
        return AppTokenCandidate(
            identity=self.credential_identity,
            repository=repository,
            token=f"app-{repository.repository_id}-{len(self.calls)}",
            expires_at=self.now[0] + 3600,
        )

    async def mint_for_repository(self, full_name: str) -> AppTokenCandidate:
        repository = REPOSITORY if full_name == REPOSITORY.full_name else OTHER
        return await self.mint(repository)


@pytest.fixture(params=["app", "existing_login"])
def workflow(tmp_path: Path, request):
    executable = tmp_path / "gh"
    executable.write_text(FAKE_GH)
    executable.chmod(0o700)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "operator_config": str(tmp_path / "operator-gh"),
        "heads": {"main": BASE, "aq/task": HEAD},
        "pull": None,
    }))
    log_path = tmp_path / "events.jsonl"
    now = [1_800_000_000.0]
    provider = InstallationProvider(now)
    env = {
        "AQ_FAKE_GH_STATE": str(state_path),
        "AQ_FAKE_GH_LOG": str(log_path),
        "AQ_FAKE_STORED_LOGIN": "1",
        "GH_CONFIG_DIR": str(tmp_path / "operator-gh"),
        "GH_TOKEN": "github_pat_poisoned_primary",
        "GITHUB_TOKEN": "github_pat_poisoned_secondary",
        "PATH": os.environ["PATH"],
    }
    config = (
        GitHubAppConfig("Iv1.client", 101, 202, str(tmp_path / "unused-key.pem"))
        if request.param == "app" else None
    )
    access = GitHubAccess.from_config(
        config, app_provider=provider if config else None,
        clock=lambda: now[0], executable=str(executable), env=env, cwd=tmp_path,
    )
    return access, provider, now, env, state_path, log_path, request.param


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True, capture_output=True,
    )
    return result.stdout.strip()


@pytest.mark.asyncio
async def test_private_repository_pr_ci_merge_workflow_has_credential_parity(workflow):
    access, provider, _, env, state_path, log_path, mode = workflow
    original_env = dict(env)
    binding = await access.bind_repository("https://github.com/acme/widgets.git")
    assert binding == REPOSITORY
    assert access.credential_identity == (
        GitHubCredentialIdentity.app(101, 202)
        if mode == "app" else GitHubCredentialIdentity.existing_login()
    )
    client = GitHubClient(binding, access=access)

    assert await client.exact_head_ref("aq/task") == HEAD
    assert await client.create_pull_request(
        title="Private delivery", body="private fixture delivery", base="main", head="aq/task"
    ) == PR_URL
    assert (await client.pull_request(PR_URL))["head"]["sha"] == HEAD
    assert (await client.check_rollup(PR_URL))[0]["conclusion"] == "SUCCESS"
    assert await client.commit_head("main") == BASE
    assert (await client.commit_check_runs(HEAD))[0]["conclusion"] == "success"
    assert (await client.compare("main", HEAD))["behind_by"] == 0
    assert await client.job_log(17) == "required: pass\n"
    with pytest.raises(GitHubAccessError):
        await client.pull_request("https://github.com/foreign/repo/pull/7")
    assert await client.merge_pull_request(
        PR_URL, method="squash", expected_head_oid=HEAD, expected_base_ref="main"
    ) == MERGE
    assert await client.exact_head_ref("aq/task") is None
    assert json.loads(state_path.read_text())["heads"]["main"] == MERGE

    events = _events(log_path)
    assert sum(e["argv"][:2] == ["pr", "create"] for e in events) == 1
    assert sum(e["argv"][:2] == ["pr", "merge"] for e in events) == 1
    assert all(e["repo"] in (None, "acme/widgets") for e in events)
    assert all(e["kind"] == ("app" if mode == "app" else "login") for e in events)
    if mode == "app":
        assert provider.calls
        assert all(e["config_isolated"] and not e["has_secondary_token"] for e in events)
    else:
        assert provider.calls == []
        assert all(not e["config_isolated"] and e["has_secondary_token"] for e in events)
    assert env == original_env


@pytest.mark.asyncio
async def test_app_denial_never_uses_available_personal_token(workflow):
    access, provider, _, _, state_path, log_path, mode = workflow
    if mode != "app":
        pytest.skip("App authority is the denial under test")
    provider.fail = True
    with pytest.raises(GitHubAccessError, match="installation permission denied"):
        await access.bind_repository("https://github.com/acme/widgets")
    assert _events(log_path) == []

    provider.fail = False
    state = json.loads(state_path.read_text())
    state["deny_app"] = True
    state_path.write_text(json.dumps(state))
    with pytest.raises(GitHubAccessError):
        await access.bind_repository("https://github.com/acme/widgets")
    assert _events(log_path)
    assert all(event["kind"] == "app" for event in _events(log_path))


@pytest.mark.asyncio
async def test_app_expiry_and_simultaneous_repositories_keep_tokens_separate(workflow):
    access, provider, now, env, _, log_path, mode = workflow
    if mode != "app":
        pytest.skip("App token lifecycle is the subject")
    original_env = dict(env)
    first, second = await asyncio.gather(
        access.bind_repository("acme/widgets"),
        access.bind_repository("acme/gadgets"),
    )
    assert (first, second) == (REPOSITORY, OTHER)
    client = GitHubClient(first, access=access)
    await client.exact_head_ref("main")
    now[0] += 3400  # inside the 300-second refresh window
    await asyncio.gather(client.exact_head_ref("main"), client.commit_head("main"))
    assert provider.calls.count(REPOSITORY) == 2
    assert provider.calls.count(OTHER) == 1
    events = _events(log_path)
    assert {e["repo"] for e in events if e["repo"]} == set()
    assert {e["generation"] for e in events} == {"1", "2", "3"}
    assert all(e["kind"] == "app" and e["config_isolated"] for e in events)
    assert env == original_env


@pytest.mark.asyncio
async def test_integration_audit_publication_reconciles_by_idempotency_key(workflow):
    access, _, _, _, state_path, log_path, mode = workflow
    state = json.loads(state_path.read_text())
    branch = "aq/integration/batch"
    state["heads"][branch] = HEAD
    state_path.write_text(json.dumps(state))
    binding = await access.bind_repository("acme/widgets")
    client = GitHubClient(binding, access=access)
    key = "d" * 64
    created = await client.create_audit_pr(
        repository_id="private-fixture", branch=branch, head_sha=HEAD,
        base_branch="main", batch_id="batch", idempotency_key=key,
        repository_numeric_id=303, repository_full_name="acme/widgets",
    )
    assert created.number == 8
    assert created.head_sha == HEAD
    assert (await client.lookup_audit_pr(idempotency_key=key, branch=branch)) == created
    repeated = await client.create_audit_pr(
        repository_id="private-fixture", branch=branch, head_sha=HEAD,
        base_branch="main", batch_id="batch", idempotency_key=key,
        repository_numeric_id=303, repository_full_name="acme/widgets",
    )
    assert repeated == created
    events = _events(log_path)
    assert sum(
        e["argv"][0] == "api" and "POST" in e["argv"] for e in events
    ) == 1
    assert all(e["kind"] == ("app" if mode == "app" else "login") for e in events)


@pytest.mark.asyncio
async def test_prime_worker_push_and_pr_use_held_branch_and_shared_credential(
    workflow, tmp_path, internal_plugins_handler,
):
    """Drive the command plugin as a worker against local Git and fake gh."""
    access, _, _, _, state_path, log_path, mode = workflow
    remote = tmp_path / "private.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    checkout = tmp_path / "worker"
    checkout.mkdir()
    _git(checkout, "init", "--initial-branch=main")
    _git(checkout, "config", "user.name", "Workflow Test")
    _git(checkout, "config", "user.email", "workflow@example.invalid")
    (checkout / "file.txt").write_text("base\n")
    _git(checkout, "add", "file.txt")
    _git(checkout, "commit", "-m", "base")
    _git(checkout, "remote", "add", "origin", str(remote))
    _git(checkout, "push", "origin", "main")
    _git(checkout, "fetch", "origin", "main")
    _git(checkout, "checkout", "-b", "aq/task")
    (checkout / "file.txt").write_text("delivery\n")
    _git(checkout, "commit", "-am", "delivery")
    tip = _git(checkout, "rev-parse", "HEAD")
    state = json.loads(state_path.read_text())
    state["heads"]["main"] = _git(checkout, "rev-parse", "main")
    state["heads"].pop("aq/task")
    state_path.write_text(json.dumps(state))

    db = Database(lease_dsn("github-access-workflow.db"))
    await db.initialize()
    try:
        await db.create_project(Project(
            id="p", name="Private fixture", repo_url="https://github.com/acme/widgets.git",
            repo_default_branch="main",
        ))
        await db.upsert_profile(AgentProfile(
            id="coder", name="coder", harness="codex", needs_workspace=False,
            default_class="standard-low",
        ))
        await db.create_agent(Agent(id="a1", name="a1", profile_id="coder"))
        await db.create_task(Task(
            id="t1", project_id="p", title="Private delivery", description="Deliver fixture",
            status=TaskStatus.IN_PROGRESS, profile_id="coder", assigned_agent_id="a1",
            branch_name="aq/task",
        ))
        await db.update_agent("a1", state=AgentState.BUSY, current_task_id="t1")
        await db.create_session(SessionRecord(
            id="s1", task_id="t1", project_id="p", agent_id="a1", profile_id="coder",
            harness="codex", provider="fake", name="s1", lifecycle="pool",
            state="running", work_dir=str(checkout), epoch="test",
            instance_token="instance-a1", started_at=time.time(), last_claim_epoch=0,
        ))
        await db.create_workspace(Workspace(
            id="base", project_id="p", workspace_path=str(checkout),
            source_type=RepoSourceType.LINK,
        ))
        git = GitManager(github_access=access)
        transfers = []

        async def local_transfer(work_dir, base_ref, source, target, **kwargs):
            transfers.append((work_dir, base_ref, source, target, kwargs))
            assert work_dir == str(checkout)
            assert base_ref == "refs/remotes/origin/main"
            assert (source, target) == ("aq/task", "aq/task")
            assert kwargs["repository_url"] == "https://github.com/acme/widgets.git"
            expected = kwargs["expected_remote_oid"]
            if expected is not None:
                assert _git(remote, "rev-parse", f"refs/heads/{target}") == expected
            lease = ([f"--force-with-lease=refs/heads/{target}:{expected}"]
                     if expected is not None else [])
            _git(checkout, "push", *lease, str(remote), f"{source}:refs/heads/{target}")
            pushed_oid = _git(checkout, "rev-parse", "HEAD")
            state = json.loads(state_path.read_text())
            state["heads"][target] = pushed_oid
            state_path.write_text(json.dumps(state))
            return pushed_oid

        git.apush_validated_delivery = local_transfer
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            workspace_dir=str(tmp_path),
            database=DatabaseConfig(url=lease_dsn("github-access-workflow.db")),
            data_dir=str(tmp_path / "data"),
        )
        handler = await internal_plugins_handler(db=db, config=config, git=git)
        scoped = {"_scope": {
            "kind": "session", "session_id": "s1", "project_id": "p",
        }}
        prime = await handler.execute("prime", dict(scoped))
        assert prime["success"] is True
        assert "Private delivery" in prime["body"]
        assert "aq/task" in prime["body"]

        refused = await handler.execute("git_push", {**scoped, "branch": "aq/foreign"})
        assert "error" in refused
        assert transfers == []
        pushed = await handler.execute("git_push", dict(scoped))
        assert pushed["oid"] == tip
        assert _git(remote, "rev-parse", "refs/heads/aq/task") == tip
        (checkout / "file.txt").write_text("recovery delivery\n")
        _git(checkout, "commit", "-am", "recovery")
        recovery_tip = _git(checkout, "rev-parse", "HEAD")
        recovered = await handler.execute("git_push", {
            **scoped, "expected_remote_oid": tip,
        })
        assert recovered["oid"] == recovery_tip
        assert _git(remote, "rev-parse", "refs/heads/aq/task") == recovery_tip
        created = await handler.execute("git_create_pr", {
            **scoped, "title": "Private delivery", "body": "private fixture delivery",
        })
        assert created["pr_url"] == PR_URL
        assert any(e["argv"][:2] == ["pr", "create"] for e in _events(log_path))
        assert all(e["kind"] == ("app" if mode == "app" else "login") for e in _events(log_path))
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_operator_onboards_private_url_through_shared_access(
    workflow, tmp_path, internal_plugins_handler,
):
    access, _, _, _, _, log_path, mode = workflow
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.name", "Workflow Test")
    _git(seed, "config", "user.email", "workflow@example.invalid")
    (seed / "README.md").write_text("disposable private fixture\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    remote = tmp_path / "private.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(remote)],
        check=True, text=True, capture_output=True,
    )
    root = tmp_path / "root"
    root.mkdir()

    class LocalGitEndpoint(GitManager):
        def __init__(self):
            super().__init__(github_access=access)
            self.selections = []

        async def _aclone_with_auth_to_url(
            self, configured_url, checkout_path, *, source_url, token,
            bare=False, no_checkout=False,
        ):
            assert source_url == "https://github.com/acme/widgets.git"
            assert not bare and not no_checkout
            self.selections.append((configured_url, bool(token)))
            await self._arun(["clone", str(remote), checkout_path])
            await self._arun(["remote", "set-url", "origin", configured_url], cwd=checkout_path)

    git = LocalGitEndpoint()
    db = Database(lease_dsn("github-access-onboarding.db"))
    await db.initialize()
    try:
        config = AppConfig(
            discord=DiscordConfig(bot_token="test-token", guild_id="123"),
            project_roots=[ProjectRoot(id="fixture", label="Fixture", path=str(root))],
            workspace_dir=str(tmp_path / "workspaces"),
            database=DatabaseConfig(url=lease_dsn("github-access-onboarding.db")),
            data_dir=str(tmp_path / "data"),
        )
        handler = await internal_plugins_handler(db=db, config=config, git=git)
        handler.orchestrator.github_access = access
        result = await handler.execute("onboard_project", {
            "_scope": {"kind": "local"},
            "request_id": "private-url-fixture", "source_mode": "github_clone",
            "root_id": "fixture", "relative_path": "private", "project_id": "private",
            "project_name": "Private fixture", "github_url": "https://github.com/acme/widgets.git",
        })
        assert result["success"] is True, result
        assert _git(root / "private", "rev-parse", "HEAD") == _git(seed, "rev-parse", "HEAD")
        assert git.selections == [("https://github.com/acme/widgets.git", mode == "app")]
        assert _events(log_path)
        assert all(e["kind"] == ("app" if mode == "app" else "login") for e in _events(log_path))
    finally:
        await db.close()
