"""PR intent commands over real PostgreSQL and Git, with isolated GitHub transports."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from sqlalchemy import insert, select, text, update

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.commands.promote_commands import PromoteCommandsMixin
from src.database.tables import (
    integration_batches,
    projects,
    repos,
    task_branch_origins,
    task_context,
    tasks,
)
from src.git.github_contracts import GitHubAccessError, GitHubCredentialIdentity
from src.integration.batches import BatchStore
from src.integration.ci import ATTESTATION_CHECK_NAME, IntegrationTrustManifest
from src.integration.delivery_observer import delivery_targets
from src.integration.promotion_steps import cache_promotion_review, promotion_ref
from src.integration.train import TrainLane
from src.profiles.capabilities import DENY_ALL
from tests.test_integration_gitops import commit, git
from tests.test_integration_gitops import setup as setup
from tests.test_promotion_steps import PromotionGitHub
from tests.test_promotion_steps import promotion as promotion


class IntentGitHub(PromotionGitHub):
    def __init__(self, env):
        super().__init__(env.source, "aq/promote/release/0.2.0", "main")
        self.env = env
        self.reviews = []
        self.created = 0
        self.fail_pull_once = False

    async def create_pull_request(self, *, head, base, **kwargs):
        self.body = kwargs["body"]
        if not self.created:
            self.created += 1
            self.pull["head"].update(
                ref=head, sha=git(self.env.ops.git.remote_path, "rev-parse", "refs/heads/" + head)
            )
            self.pull["base"]["ref"] = base
        return self.pull["html_url"]

    async def close_pull_request(self, *, number):
        assert number == 42
        self.pull["state"] = "closed"

    async def request_json(self, method, path, **kwargs):
        if method == "GET" and path.endswith("/pulls/42"):
            if self.fail_pull_once:
                self.fail_pull_once = False
                raise GitHubAccessError("unavailable", "lost PR response")
            if git(self.env.ops.git.remote_path, "rev-parse", "main") == self.pull["head"]["sha"]:
                self.pull.update(state="closed", merged=True)
        return await super().request_json(method, path, **kwargs)


class HumanGitHub:
    credential_identity = GitHubCredentialIdentity.existing_login()

    def __init__(self, app):
        self.app = app
        self.login = "operator"
        self.kind = "User"
        self.posts = 0

    async def authenticated_user(self):
        return {"login": self.login, "type": self.kind}

    async def paged_list(self, path):
        return await self.app.paged_list(path)

    async def request_json(self, method, path, **kwargs):
        if method == "POST" and path.endswith("/reviews"):
            self.posts += 1
            body = kwargs["json_body"]
            assert body["event"] == "APPROVE"
            review = self.app.review(len(self.app.reviews) + 1, body["commit_id"], login=self.login)
            self.app.reviews.append(review)
            return copy.deepcopy(review)
        return await self.app.request_json(method, path, **kwargs)


@pytest.fixture
async def promote_env(promotion):
    e = promotion

    async def local_fetch(store, *, repository, oid, destination_ref):
        assert repository == e.repo.binding
        git(store, "fetch", str(e.ops.git.remote_path), oid + ":" + destination_ref)
        return oid

    e.ops.git.afetch_repository_oid = local_fetch
    source = commit(
        e.repo.store, {"pyproject.toml": '[project]\nversion = "0.2.0"\n'}, base=e.source
    )
    git(e.repo.store, "push", "origin", source + ":refs/heads/dev")
    async with e.db._engine.begin() as conn:
        # Reset the prerequisite fixture before creating command-authored intents.
        # TRUNCATE preserves the immutable-membership trigger used by the tests.
        await conn.execute(
            text(
                "TRUNCATE integration_batch_members, integration_batches, "
                "task_context, tasks CASCADE"
            )
        )
        await conn.execute(
            update(projects).values(integration_repository_id="r", promotion_flow=[e.meta["step"]])
        )
        await conn.execute(update(repos).values(default_branch="dev"))
    e.source = source
    e.github = IntentGitHub(e)
    e.github.runs[source] = "success"
    e.human = HumanGitHub(e.github)
    e.trust = IntegrationTrustManifest(
        schema="aq.integration-trust.v1",
        canonical_repository_id="r",
        repository_id=123,
        full_name="test/repo",
        ci_producer_app_id=15368,
        attestation_app_id=101,
        attestation_name=ATTESTATION_CHECK_NAME,
        promotion_attestation_names=[e.meta["step"]["gate"]["attestation"]],
        required_checks={"version": "checks-v1", "names": ["unit"]},
    )
    handler = PromoteCommandsMixin()
    handler.db = e.db
    lane = TrainLane(snapshot=e.snapshot, service=e.service, checks=e.checks)
    handler.orchestrator = SimpleNamespace(
        integration_train=SimpleNamespace(
            lane_for=AsyncMock(return_value=lane), wake=Mock(return_value=1),
        ),
        github_client_factory=lambda _: e.github,
        promotion_user_client_factory=lambda _: e.human,
    )
    handler._promotion_manifest = AsyncMock(
        return_value=e.trust.model_dump(mode="json", by_alias=True)
    )
    e.handler = handler
    return e


async def request(e, **kwargs):
    return await e.handler._cmd_promote_request({"project_id": "p", "step_id": "release", **kwargs})


async def test_request_freezes_identity_retains_source_and_replays(promote_env):
    e = promote_env
    first, second = await asyncio.gather(request(e), request(e))
    assert {first["outcome"], second["outcome"]} == {"requested", "already_requested"}, (
        first,
        second,
    )
    meta = first["promotion"]
    assert meta["source_sha"] == e.source and meta["base_sha"] == e.base
    assert meta["version"] == "0.2.0" and meta["requester"]["github_login"] == "operator"
    assert meta["check_names"] == ["unit"] and meta["checks_version"] == "checks-v1"
    assert meta["notes_sha256"] is None
    assert e.github.created == 1
    assert git(e.ops.git.remote_path, "rev-parse", promotion_ref(meta["step"], meta)) == e.source
    batch = await e.store.get(first["batch_id"])
    members = await e.store.members(batch.id)
    assert await e.admission.load(batch, members) == meta
    async with e.db._engine.connect() as conn:
        row = (await conn.execute(select(tasks))).mappings().one()
        assert row["task_type"] == "promotion" and row["profile_id"] is None
        assert json.loads(await conn.scalar(select(task_context.c.content))) == meta
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


async def test_ambiguous_pr_response_recovers_one_request(promote_env):
    e = promote_env
    e.github.fail_pull_once = True
    assert (await request(e))["outcome"] == "unavailable"
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.id)) is None
    assert (await request(e))["outcome"] == "requested"
    assert e.github.created == 1


async def test_competing_versions_cannot_open_two_intents(promote_env):
    e = promote_env
    other = commit(
        e.repo.store, {"pyproject.toml": '[project]\nversion = "0.2.1"\n'}, base=e.source
    )
    git(e.repo.store, "push", "origin", other + ":refs/heads/dev")
    e.github.runs[other] = "success"
    results = await asyncio.gather(request(e, source_sha=e.source), request(e, source_sha=other))
    assert {r["outcome"] for r in results} == {"requested", "promotion_in_progress"}, results
    assert e.github.created == 1


async def test_approve_posts_human_review_without_mutating_frozen_intent(promote_env):
    e = promote_env
    created = await request(e)
    args = {"project_id": "p", "request_id": created["request_id"]}
    approved = await e.handler._cmd_promote_approve(args)
    assert approved["outcome"] == "approved", approved
    assert approved["review"]["commit_id"] == e.source
    assert (await e.handler._cmd_promote_approve(args))["outcome"] == "already_approved"
    assert e.human.posts == 1
    async with e.db._engine.connect() as conn:
        assert json.loads(await conn.scalar(select(task_context.c.content))) == created["promotion"]
        assert await conn.scalar(select(integration_batches.c.intent)) == "open"
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="worker", project_id="p"
    )
    with principal_context(worker):
        assert (await e.handler._cmd_promote_approve(args))["outcome"] == "unauthorized"


@pytest.mark.parametrize("change", ["app", "bot", "permission", "moved", "no_login"])
async def test_approve_refuses_wrong_credentials_permissions_or_head(promote_env, change):
    e = promote_env
    created = await request(e)
    if change == "no_login":
        e.human.login = None
    elif change == "app":
        e.human.credential_identity = e.github.credential_identity
    elif change == "bot":
        e.human.kind = "Bot"
    elif change == "permission":
        e.github.admins.clear()
    else:
        e.github.pull["head"]["sha"] = e.base
    result = await e.handler._cmd_promote_approve(
        {"project_id": "p", "request_id": created["request_id"]}
    )
    assert not result["success"] and e.human.posts == 0, result
    if change == "no_login":
        assert result["outcome"] == "unauthorized", result


async def test_cache_read_includes_veto_and_never_calls_github(promote_env):
    e = promote_env
    created = await request(e)
    # Another reviewer's changes request is advisory in requester mode.
    e.github.reviews.append(
        e.github.review(1, e.source, login="reviewer", state="CHANGES_REQUESTED")
    )
    assert (
        await e.handler._cmd_promote_approve(
            {"project_id": "p", "request_id": created["request_id"]}
        )
    )["success"]
    e.handler.orchestrator.github_client_factory = lambda _: pytest.fail(
        "cache read contacted GitHub"
    )
    e.handler._promotion_manifest = AsyncMock(side_effect=AssertionError("read fetched trust"))
    status = await e.handler._cmd_promote_status({"project_id": "p", "step_id": "release"})
    assert status["promotions"][0]["review"]["verdict"] == "approved"
    # A later lane observation of the requester's own veto supersedes the approval.
    [member] = await BatchStore(e.db).members(created["batch_id"])
    async with e.db.immediate() as conn:
        await cache_promotion_review(
            conn, "r", member, git(e.repo.store, "rev-parse", e.source + "^{tree}"),
            "promotion_pr_changes_requested", {"observed_by": "lane"},
        )
    for method in (e.handler._cmd_promote_status, e.handler._cmd_promote_list):
        result = await method({"project_id": "p", "step_id": "release"})
        assert result["success"] and result["evidence_source"] == "cache"
        [entry] = result["promotions"]
        assert entry["review"]["evidence"]["reason"] == "promotion_pr_changes_requested"
        assert entry["review"]["verdict"] == "rejected"


async def test_cancel_closes_pr_then_aborts_and_replays(promote_env):
    e = promote_env
    created = await request(e)
    args = {"project_id": "p", "request_id": created["request_id"]}
    result = await e.handler._cmd_promote_cancel(args)
    assert result["outcome"] == "cancelled", result
    assert e.github.pull["state"] == "closed"
    assert (await e.handler._cmd_promote_cancel(args))["outcome"] == "already_cancelled"
    assert (await e.store.get(created["batch_id"])).intent == "aborted"
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.base
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


async def test_changing_commands_wake_only_their_own_promotion_target(promote_env):
    """grand-lantern-78.4: a held promotion revisits at once after a promote command."""
    e = promote_env
    wake = e.handler.orchestrator.integration_train.wake
    created = await request(e)
    target = call("p", "r", "refs/heads/" + e.meta["step"]["target"])
    assert wake.call_args_list == [target]
    args = {"project_id": "p", "request_id": created["request_id"]}
    assert (await e.handler._cmd_promote_approve(args))["outcome"] == "approved"
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="worker", project_id="p"
    )
    with principal_context(worker):
        assert not (await e.handler._cmd_promote_cancel(args))["success"]
    assert (await e.handler._cmd_promote_status({"project_id": "p"}))["success"]
    assert (await e.handler._cmd_promote_cancel(args))["outcome"] == "cancelled"
    assert wake.call_args_list == [target] * 3


async def test_cancel_refuses_after_target_publish(promote_env):
    e = promote_env
    created = await request(e)
    git(e.repo.store, "push", "origin", e.source + ":main")
    result = await e.handler._cmd_promote_cancel(
        {"project_id": "p", "request_id": created["request_id"]}
    )
    assert result["outcome"] == "promotion_publish_started", result
    assert (await e.store.get(created["batch_id"])).intent == "open"


@pytest.mark.parametrize("command", ["request", "approve", "cancel", "status", "list"])
async def test_commands_refuse_unconfigured_flow(promote_env, command):
    e = promote_env
    async with e.db._engine.begin() as conn:
        await conn.execute(update(projects).values(promotion_flow=[]))
    args = (
        {"project_id": "p", "step_id": "release"}
        if command in {"request", "status", "list"}
        else {"project_id": "p", "request_id": "promotion:r:release:0.2.0"}
    )
    result = await getattr(e.handler, "_cmd_promote_" + command)(args)
    assert result["outcome"] == "promotion_flow_empty"


async def test_notes_acknowledgement_and_hash_use_pinned_bytes(promote_env):
    e = promote_env
    step = copy.deepcopy(e.meta["step"])
    step["notes"] = {"kind": "file_template", "path": "notes/{version}.md"}
    body = "---\nsource_digest: " + hashlib.sha256(b"").hexdigest() + "\n---\nRelease notes\n\n"
    source = commit(e.repo.store, {"notes/0.2.0.md": body}, base=e.source)
    e.github.runs[source] = "success"
    git(e.repo.store, "push", "origin", source + ":refs/heads/dev")
    async with e.db._engine.begin() as conn:
        await conn.execute(update(projects).values(promotion_flow=[step]))
    assert (await request(e))["outcome"] == "notes_not_reviewed"
    result = await request(e, notes_reviewed=True)
    assert result["success"], result
    assert result["promotion"]["notes_sha256"] == hashlib.sha256(body.encode()).hexdigest()


@pytest.mark.parametrize(
    "parent,retired,repository,expected",
    [
        ("main", None, "r", "main"),
        ("refs/heads/main", None, "r", "main"),
        ("unrelated", None, "r", "dev"),
        ("main", 2, "r", "dev"),
        ("main", None, "other", "dev"),
    ],
)
async def test_delivery_routing_uses_only_live_same_repository_chain_origin(
    promote_env,
    parent,
    retired,
    repository,
    expected,
):
    e = promote_env
    async with e.db._engine.begin() as conn:
        await conn.execute(
            insert(tasks).values(
                id="hotfix",
                project_id="p",
                repo_id="r",
                title="hotfix",
                description="",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id="hotfix",
                repository_id=repository,
                parent_ref=parent,
                base_sha=e.base,
                creation_generation=1,
                created_at=1,
                retired_at=retired,
            )
        )
        targets = await delivery_targets(conn, ["hotfix"])
    assert targets["hotfix"].target_ref == "refs/heads/" + expected


async def test_cancel_refuses_active_publication_lease(promote_env):
    from src.integration.lock import BranchLock
    from src.integration.models import BranchKey

    e = promote_env
    created = await request(e)
    locks = BranchLock(e.db)
    fence = await locks.acquire(
        BranchKey(repository_id="r", branch="refs/heads/main"), "publisher", role="integration"
    )
    try:
        result = await e.handler._cmd_promote_cancel(
            {"project_id": "p", "request_id": created["request_id"]}
        )
        assert result["outcome"] == "promotion_publish_started", result
        assert e.github.pull["state"] == "open"
    finally:
        await locks.release(fence)


async def test_failed_pr_close_does_not_abort_intent(promote_env):
    e = promote_env
    created = await request(e)
    e.github.close_pull_request = AsyncMock(
        side_effect=GitHubAccessError("transient", "unavailable")
    )
    result = await e.handler._cmd_promote_cancel(
        {"project_id": "p", "request_id": created["request_id"]}
    )
    assert result["outcome"] == "unavailable"
    assert (await e.store.get(created["batch_id"])).intent == "open"


async def test_request_refuses_existing_tag_and_version_mismatch(promote_env):
    e = promote_env
    assert (await request(e, version="9.9.9"))["outcome"] == "version_mismatch"
    git(e.repo.store, "tag", "v0.2.0", e.source)
    git(e.repo.store, "push", "origin", "refs/tags/v0.2.0")
    assert (await request(e))["outcome"] == "tag_exists"
    assert e.github.created == 0


def test_intent_contracts_have_typed_arguments_and_read_only_cache_commands():
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.models import SideEffectClass

    for name in ("prepare", "request", "hotfix", "approve", "cancel", "status", "list"):
        registration = CONTRACTS.get("promote_" + name)
        assert registration is not None
        contract = registration.contract.execution
        assert contract.capability == "promote_" + name
        assert contract.side_effect is (
            SideEffectClass.READ if name in {"status", "list"} else SideEffectClass.COMPOSITE
        )
    with pytest.raises(ValueError, match="exact lowercase Git OID"):
        CONTRACTS.get("promote_request").contract.execution.args_model.model_validate(
            {"project_id": "p", "step_id": "release", "source_sha": "HEAD"}
        )


@pytest.mark.parametrize(
    "name,argv,args",
    [
        (
            "prepare", ["--step", "release", "--bump", "minor", "--from-task", "feature"],
            {"step_id": "release", "bump": "minor", "version": None, "from_task": "feature"},
        ),
        (
            "request",
            ["--step", "release", "--from", "a" * 40, "--version", "0.2.0", "--notes-reviewed"],
            {
                "step_id": "release",
                "source_sha": "a" * 40,
                "version": "0.2.0",
                "notes_reviewed": True,
            },
        ),
        (
            "request", ["--step", "release", "--from-task", "fix"],
            {"step_id": "release", "from_task": "fix", "source_sha": None,
             "version": None, "notes_reviewed": False},
        ),
        (
            "hotfix", ["--step", "release", "--title", "Fix release", "--version", "0.2.1"],
            {"step_id": "release", "title": "Fix release", "version": "0.2.1",
             "description": None, "from_task": None},
        ),
        ("approve", ["promotion:r:release:0.2.0"], {"request_id": "promotion:r:release:0.2.0"}),
        ("cancel", ["promotion:r:release:0.2.0"], {"request_id": "promotion:r:release:0.2.0"}),
        ("status", ["--step", "release"], {"step_id": "release"}),
        ("list", ["--limit", "5"], {"step_id": None, "limit": 5}),
    ],
)
def test_intent_cli_forwards_exact_arguments(name, argv, args):
    from contextlib import asynccontextmanager
    from unittest.mock import patch

    from click.testing import CliRunner

    from src.cli.app import cli

    client = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True, "outcome": "requested"})
    )

    @asynccontextmanager
    async def get_client():
        yield client

    with patch("src.cli.promote._get_client", get_client):
        result = CliRunner().invoke(cli, ["--json", "promote", name, "--project", "p", *argv])
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once_with("promote_" + name, {"project_id": "p", **args})


async def test_requester_gate_binds_authenticated_principal_and_human_login(promote_env):
    from src.profiles.capabilities import CapabilityPolicy

    e = promote_env
    step = copy.deepcopy(e.meta["step"])
    step["gate"]["approval"] = "requester"
    async with e.db._engine.begin() as conn:
        await conn.execute(update(projects).values(promotion_flow=[step]))
    policy = CapabilityPolicy.from_namespaces(
        aq_commands=["promote_request", "promote_approve", "promote_cancel"],
        harness_tools=[],
        plugin_tools=[],
    )
    requester = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=policy, session_id="requester", project_id="p"
    )
    with principal_context(requester):
        # No verified GitHub binding for the session: nothing is opened.
        missing = await request(e)
        assert missing["outcome"] == "promotion_requester_identity_missing", missing
        assert e.github.created == 0
    e.handler.orchestrator.promotion_requester_login = AsyncMock(return_value="operator")
    with principal_context(requester):
        created = await request(e)
        assert created["success"], created
        assert created["promotion"]["requester"]["identity"] == requester.describe()
        # Approval posts with this host's gh login, so even the requester's
        # own session is refused; only the local human may approve.
        result = await e.handler._cmd_promote_approve(
            {"project_id": "p", "request_id": created["request_id"]}
        )
        assert result["outcome"] == "unauthorized", result
    # The local operator is not this step's requester.
    assert (
        await e.handler._cmd_promote_approve(
            {"project_id": "p", "request_id": created["request_id"]}
        )
    )["outcome"] == "unauthorized"
    assert e.human.posts == 0
    other = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=policy, session_id="other", project_id="p"
    )
    with principal_context(other):
        assert (
            await e.handler._cmd_promote_cancel(
                {"project_id": "p", "request_id": created["request_id"]}
            )
        )["outcome"] == "unauthorized"
    with principal_context(requester):
        cancelled = await e.handler._cmd_promote_cancel(
            {"project_id": "p", "request_id": created["request_id"]}
        )
        assert cancelled["outcome"] == "cancelled", cancelled


async def test_local_requester_approves_requester_step(promote_env):
    e = promote_env
    step = copy.deepcopy(e.meta["step"])
    step["gate"]["approval"] = "requester"
    async with e.db._engine.begin() as conn:
        await conn.execute(update(projects).values(promotion_flow=[step]))
    created = await request(e)
    assert created["promotion"]["requester"] == {
        "identity": "human:local-operator",
        "github_login": "operator",
    }
    result = await e.handler._cmd_promote_approve(
        {"project_id": "p", "request_id": created["request_id"]}
    )
    assert result["outcome"] == "approved", result
    e.human.login = "someone-else"
    assert (
        await e.handler._cmd_promote_approve(
            {"project_id": "p", "request_id": created["request_id"]}
        )
    )["outcome"] == "unauthorized"


def _supervisor(project_id=None, session_id="supervisor"):
    from src.profiles.capabilities import CapabilityPolicy

    policy = CapabilityPolicy.from_namespaces(
        aq_commands=["promote_prepare", "promote_request", "promote_cancel", "promote_status", "promote_list"],
        harness_tools=[],
        plugin_tools=[],
    )
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=policy,
        session_id=session_id,
        project_id=project_id,
        profile_id="supervisor",
        elevated=True,
    )


@pytest.mark.parametrize("project_id", [None, "p"])
async def test_live_supervisor_cannot_cancel_or_approve_another_request(
    promote_env, monkeypatch, project_id
):
    e = promote_env
    # Admit the session as a live supervisor exactly as a running one is.
    monkeypatch.setattr(
        "src.commands.promote_commands.integration_operator",
        AsyncMock(return_value=("supervisor session:supervisor", None)),
    )
    created = await request(e)
    args = {"project_id": "p", "request_id": created["request_id"]}
    with principal_context(_supervisor(project_id)):
        cancelled = await e.handler._cmd_promote_cancel(args)
        approved = await e.handler._cmd_promote_approve(args)
    assert cancelled["outcome"] == "unauthorized", cancelled
    assert approved["outcome"] == "unauthorized", approved
    assert e.github.pull["state"] == "open" and e.human.posts == 0
    assert (await e.store.get(created["batch_id"])).intent == "open"


async def test_supervisor_cancels_its_own_request_and_operator_cancels_any(promote_env):
    e = promote_env
    supervisor = _supervisor("p")
    with principal_context(supervisor):
        created = await request(e)
        assert created["promotion"]["requester"]["identity"] == supervisor.describe()
        cancelled = await e.handler._cmd_promote_cancel(
            {"project_id": "p", "request_id": created["request_id"]}
        )
    assert cancelled["outcome"] == "cancelled", cancelled
    # A request opened by another session is still the local operator's to cancel.
    other = commit(
        e.repo.store, {"pyproject.toml": '[project]\nversion = "0.2.1"\n'}, base=e.source
    )
    git(e.repo.store, "push", "origin", other + ":refs/heads/dev")
    e.github.runs[other] = "success"
    e.github.created = 0
    e.github.pull["state"] = "open"
    with principal_context(_supervisor("p", session_id="another")):
        second = await request(e, source_sha=other)
    assert second["success"], second
    result = await e.handler._cmd_promote_cancel(
        {"project_id": "p", "request_id": second["request_id"]}
    )
    assert result["outcome"] == "cancelled", result


async def test_request_after_cancel_refuses_reused_identity(promote_env):
    e = promote_env
    created = await request(e)
    args = {"project_id": "p", "request_id": created["request_id"]}
    assert (await e.handler._cmd_promote_cancel(args))["outcome"] == "cancelled"
    again = await request(e)
    assert again["success"] is False and again["outcome"] == "promotion_not_open", again
    assert created["request_id"] in again["error"]
    assert e.github.created == 1


@pytest.mark.parametrize("command", ["request", "approve", "cancel"])
async def test_rate_limits_keep_category_and_retry_at(promote_env, command):
    e = promote_env
    limited = GitHubAccessError("rate_limited", "secondary rate limit", retry_at=1234.5)
    if command == "request":
        e.github.create_pull_request = AsyncMock(side_effect=limited)
        result = await request(e)
    else:
        created = await request(e)
        args = {"project_id": "p", "request_id": created["request_id"]}
        if command == "approve":
            e.human.authenticated_user = AsyncMock(side_effect=limited)
        else:
            e.github.close_pull_request = AsyncMock(side_effect=limited)
        result = await getattr(e.handler, "_cmd_promote_" + command)(args)
    assert result == {
        "success": False,
        "outcome": "rate_limited",
        "error": "secondary rate limit",
        "retry_at": 1234.5,
    }


async def test_request_holds_no_project_row_lock_across_provider_calls(promote_env):
    e = promote_env
    create = e.github.create_pull_request
    observed = []

    async def create_while_probing(**kwargs):
        # Any project-row lock taken by the request would make NOWAIT fail here.
        async with e.db._engine.begin() as conn:
            observed.append(
                await conn.scalar(
                    select(projects.c.id).where(projects.c.id == "p").with_for_update(nowait=True)
                )
            )
        return await create(**kwargs)

    e.github.create_pull_request = create_while_probing
    result = await request(e)
    assert result["outcome"] == "requested", result
    assert observed == ["p"]


async def test_flow_edit_during_provider_calls_rolls_request_back(promote_env):
    e = promote_env
    create = e.github.create_pull_request

    async def create_then_edit(**kwargs):
        url = await create(**kwargs)
        step = copy.deepcopy(e.meta["step"])
        step["gate"]["approval"] = "none"
        async with e.db._engine.begin() as conn:
            await conn.execute(update(projects).values(promotion_flow=[step]))
        return url

    e.github.create_pull_request = create_then_edit
    result = await request(e)
    assert result["outcome"] == "promotion_flow_changed", result
    async with e.db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.id)) is None


@pytest.mark.parametrize("command", ["promote_request", "promote_hotfix", "integration_backmerge_source"])
async def test_worker_without_request_capability_cannot_open_intent(promote_env, command):
    e = promote_env
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="worker", project_id="p"
    )
    with principal_context(worker):
        args = {"project_id": "p", "step_id": "release"}
        if command == "promote_hotfix":
            args["title"] = "Unauthorized fix"
        assert (await getattr(e.handler, "_cmd_" + command)(args))["outcome"] == "unauthorized"
    assert e.github.created == 0


async def test_code_free_hotfix_completion_uses_chain_target_as_base(promote_env, monkeypatch):
    from src.integration.provenance import (
        CompletionIdentity,
        GitProvenance,
        record_worker_completion,
    )

    e = promote_env
    async with e.db._engine.begin() as conn:
        await conn.execute(
            insert(tasks).values(
                id="hotfix",
                project_id="p",
                repo_id="r",
                title="hotfix",
                description="",
                branch_name="aq/hotfix",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id="hotfix",
                repository_id="r",
                parent_ref="refs/heads/main",
                base_sha=e.base,
                creation_generation=1,
                created_at=1,
            )
        )
    monkeypatch.setattr(
        "src.integration.hierarchy.resolve_workspace_checkpoint", AsyncMock(return_value=e.base)
    )
    task = await e.db.get_task("hotfix")
    project = await e.db.get_project("p")
    await record_worker_completion(
        e.db, e.ops.git, task, project, str(e.repo.store), "close-hotfix", no_code_intent=True
    )
    record = await GitProvenance(
        e.ops.git, str(e.repo.store), repository_url=str(e.ops.git.remote_path)
    ).read_completion(CompletionIdentity("p", "r", "hotfix", "close-hotfix"))
    assert record["source_oid"] == e.base and record["artifact"] is False


async def test_request_fetches_new_remote_source_before_reading_version(promote_env, tmp_path):
    e = promote_env
    writer = tmp_path / "remote-writer"
    git(tmp_path, "clone", str(e.ops.git.remote_path), str(writer))
    git(writer, "config", "user.name", "Writer")
    git(writer, "config", "user.email", "writer@example.test")
    source = commit(writer, {"latest.txt": "new source\n"}, base=e.source)
    git(writer, "push", "origin", source + ":refs/heads/dev")
    before = await e.ops.git.arun_git_result(["cat-file", "-e", source], cwd=str(e.repo.store))
    assert before.returncode != 0
    e.github.runs[source] = "success"
    opened = await request(e, source_sha=source)
    assert opened["success"] and opened["promotion"]["source_sha"] == source, opened


async def test_approval_retry_recovers_lost_review_response(promote_env):
    e = promote_env
    created = await request(e)
    args = {"project_id": "p", "request_id": created["request_id"]}
    real = e.human.request_json

    async def lost_response(method, path, **kwargs):
        result = await real(method, path, **kwargs)
        if method == "POST":
            raise GitHubAccessError("transient", "lost successful review response")
        return result

    e.human.request_json = lost_response
    assert (await e.handler._cmd_promote_approve(args))["outcome"] == "unavailable"
    e.human.request_json = real
    assert (await e.handler._cmd_promote_approve(args))["outcome"] == "already_approved"
    assert e.human.posts == 1
