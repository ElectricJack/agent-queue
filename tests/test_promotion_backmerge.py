"""Hotfix and backmerge gates with real PostgreSQL, retained refs and Git DAGs."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.commands.handler import CommandHandler
from src.commands.promote_commands import PromotionRefusal, _guard_backmerges
from src.config import AppConfig
from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY
from src.database.tables import projects, repos, task_branch_origins
from src.integration.batches import BatchStore
from src.integration.hierarchy import HierarchyIntegration
from src.integration.promotion_steps import FlowSchema, StepPullRequestGate, backmerge_ledger
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.train import TrainTarget
from src.integration.train_sources import BackmergeAdmission
from src.models import TaskType
from tests.test_integration_gitops import commit, git, setup as setup
from tests.test_integration_train_sources import hosted_train, world as world
from tests.test_promote_commands import IntentGitHub, promote_env as promote_env, request
from tests.test_promotion_steps import promotion as promotion


class MultipleIntentGitHub(IntentGitHub):
    """Independent PR identities for a held backmerge and an ordinary route."""

    def __init__(self, env):
        super().__init__(env)
        self.by_head = {}
        self.by_number = {}

    async def create_pull_request(self, *, head, base, **kwargs):
        if head not in self.by_head:
            pull = copy.deepcopy(self.pull)
            number = 42 + len(self.by_head)
            pull.update(number=number, html_url=f"https://github.com/{self.full_name}/pull/{number}")
            pull["head"].update(ref=head, sha=git(self.env.ops.git.remote_path, "rev-parse", head))
            pull["base"]["ref"] = base
            self.by_head[head] = pull
            self.by_number[number] = pull
        return self.by_head[head]["html_url"]

    async def request_json(self, method, path, **kwargs):
        if method == "GET" and "/pulls/" in path:
            return copy.deepcopy(self.by_number[int(path.rsplit("/", 1)[1])])
        return await super().request_json(method, path, **kwargs)


@pytest.mark.parametrize("mode", ["train", "development"])
async def test_hotfix_origin_and_exact_task_head_promote_to_step_target(promote_env, mode, tmp_path):
    e = promote_env
    e.base = commit(e.repo.store, {"pyproject.toml": '[project]\nversion = "0.1.0"\n'}, base=e.base)
    git(e.repo.store, "push", "origin", f"{e.base}:main")
    # Keep the fixture's version file identical on dev, so this drill tests
    # hotfix delivery rather than the separately covered conflict/repair path.
    e.source = commit(e.repo.store, {"pyproject.toml": '[project]\nversion = "0.1.1"\n'},
                      base=e.source)
    git(e.repo.store, "push", "origin", f"{e.source}:dev")
    async with e.db.immediate() as conn:
        await conn.execute(update(projects).values(hierarchical_integration_mode=mode))
    config = AppConfig(data_dir=str(tmp_path / "data"), workspace_dir=str(tmp_path / "ws"))
    orchestrator = SimpleNamespace(db=e.db, bus=SimpleNamespace(emit=AsyncMock()),
                                  playbook_manager=None)
    filer = CommandHandler(orchestrator, config)
    service = HierarchyIntegration(e.db, default_head_resolver=lambda _repo, ref:
                                   git(e.ops.git.remote_path, "rev-parse", ref))
    filer._hierarchy_integration_service = lambda: service
    e.handler._cmd_create_task = filer._cmd_create_task
    filed = await e.handler._cmd_promote_hotfix({
        "project_id": "p", "step_id": "release", "title": "Fix the released version",
        "version": "0.1.1",
    })
    assert filed["success"], filed
    tid = filed["task_id"]
    task = await e.db.get_task(tid)
    assert task.task_type is TaskType.BUGFIX and task.profile_id is None
    async with e.db._engine.connect() as conn:
        origin = (await conn.execute(select(task_branch_origins)
                                   .where(task_branch_origins.c.task_id == tid))).mappings().one()
    assert origin["parent_ref"] == "refs/heads/main" and origin["base_sha"] == e.base
    hotfix = commit(e.repo.store, {"hotfix.txt": "fix\n",
                    "pyproject.toml": '[project]\nversion = "0.1.1"\n'}, base=e.base)
    git(e.repo.store, "push", "origin", f"{hotfix}:refs/heads/{task.branch_name}")
    await e.db.update_task(tid, status="COMPLETED")
    generation = "hotfix-close"
    await e.db.set_task_meta(tid, DEVELOPMENT_COMPLETION_ID_KEY, generation)
    await GitProvenance(e.ops.git, str(e.repo.store),
                        repository_url="https://github.com/test/repo.git").write_completion(CompletedSource(
        CompletionIdentity("p", "r", tid, generation), hotfix))
    e.github.runs[hotfix] = "success"
    wrong = await request(e, from_task=tid, source_sha=e.source)
    assert wrong["outcome"] == "promotion_source_not_on_chain"
    opened = await request(e, from_task=tid)
    assert opened["success"], opened
    assert opened["promotion"]["kind"] == "hotfix"
    assert opened["promotion"]["source_sha"] == hotfix
    assert e.github.pull["base"]["ref"] == "main"
    e.checks.pull_request = lambda _: StepPullRequestGate(e.github, e.repo.binding)
    batch = await e.store.get(opened["batch_id"])
    members = await e.store.members(batch.id)
    held = await e.service.visit(batch, members, await e.snapshot())
    assert held.state == "held" and held.detail["reason"] == "promotion_operator_approval_missing"
    approved = await e.handler._cmd_promote_approve({
        "project_id": "p", "request_id": opened["request_id"],
    })
    assert approved["success"], approved
    delivered = await e.service.visit(batch, members, await e.snapshot())
    assert delivered.state == "delivered", delivered
    assert git(e.ops.git.remote_path, "rev-parse", "main") == hotfix
    assert git(e.ops.git.remote_path, "rev-parse", "refs/tags/v0.1.1^{}") == hotfix
    assert git(e.ops.git.remote_path, "rev-parse", "dev") == e.source
    # Continue the same fixture through daemon authorship and the lower branch's
    # own PR/check gate, rather than stopping at release publication.
    fixture = SimpleNamespace(db=e.db, origin=SimpleNamespace(
        url=str(e.ops.git.remote_path), clone=e.repo.store))
    lower = await source_world(fixture, prepare=False)
    e.handler.orchestrator.integration_train = lower.train
    e.handler.orchestrator.github_client_factory = lambda _: lower.github
    authored = await e.handler._cmd_integration_backmerge_source({
        "project_id": "p", "step_id": "release",
    })
    assert authored["success"], authored
    [debt] = authored["backmerges"]
    assert debt["source_sha"] == hotfix and debt["pr_url"]
    assert (await lower.github.pull_request(debt["pr_url"]))["base"]["ref"] == "dev"
    denied = await lower.train.visit(lower.target)
    assert denied.batch_id is None
    lower.github.pr_runs[hotfix] = "success"
    lower.now[0] += 61
    lower.train.wake("p")
    merged = await lower.train.visit(lower.target)
    assert merged.state == "testing", merged
    assert merged.batch_id != opened["batch_id"]
    assert [m.task_id for m in await e.store.members(merged.batch_id)] == [debt["task_id"]]
    lower.github.runs[merged.candidate_sha] = "success"
    lower.now[0] += 61
    lower.train.wake("p")
    delivered = await lower.train.visit(lower.target)
    assert delivered.state == "delivered", delivered
    assert git(e.ops.git.remote_path, "merge-base", "--is-ancestor", hotfix, "dev") == ""
    assert git(e.ops.git.remote_path, "rev-parse", "main") == hotfix


async def source_world(world, *, prepare=True):
    """Diverged dev/main, and the daemon's real ordinary PR/check admission lane."""
    base = git(world.origin.url, "rev-parse", "main")
    main = base
    if prepare:
        git(world.origin.clone, "push", "origin", f"{base}:refs/heads/dev")
        main = commit(world.origin.clone, {"hotfix.txt": "released fix\n"}, base=base)
        git(world.origin.clone, "push", "origin", f"{main}:main")
    async with world.db.immediate() as conn:
        await conn.execute(update(repos).values(default_branch="dev"))
    now = [1000.0]
    train, github, _ = await hosted_train(world, clock=lambda: now[0])
    target = TrainTarget("p", "r", "refs/heads/dev", "root")
    lane = await train.lane_for(target)
    ops = lane.service.gitops
    destination = ops.git._apush_destination

    async def local_destination(path, remote, **kwargs):
        return await destination(path, remote, repository_url=world.origin.url)

    ops.git._apush_destination = local_destination

    async def create_pull_request(*, head, base, **kwargs):
        url = f"https://github.com/{github.full_name}/pull/{700 + len(github.pulls)}"
        github.pulls[url] = head
        return url

    github.create_pull_request = create_pull_request

    async def pull_request(url):
        branch = github.pulls[url]
        head = git(world.origin.url, "rev-parse", branch)
        return {"state": "open", "merged": False, "draft": False,
                "head": {"ref": branch, "sha": head, "repo": {"id": 123}},
                "base": {"ref": "dev", "repo": {"id": 123}}}

    github.pull_request = pull_request
    admission = BackmergeAdmission(world.db, ops)
    return SimpleNamespace(train=train, github=github, target=target, lane=lane, ops=ops,
                           admission=admission, base=base, source=main, now=now)


async def test_backmerge_uses_new_batch_and_lower_branch_pr_gate(world):
    e = await source_world(world)
    authored = await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                       e.source, e.github)
    assert authored["pr_url"] and len(e.github.pulls) == 1
    assert (await world.db.get_task(authored["task_id"])).task_type is TaskType.BACKMERGE
    denied = await e.train.visit(e.target)
    assert denied.batch_id is None and denied.detail["blockers"][0]["code"] == "awaiting_pr_checks"
    e.github.pr_runs[e.source] = "failure"
    e.now[0] += 61
    e.train.wake("p")
    denied = await e.train.visit(e.target)
    assert denied.batch_id is None
    e.github.pr_runs[e.source] = "success"
    e.now[0] += 121
    e.train.wake("p")
    admitted = await e.train.visit(e.target)
    assert admitted.state == "testing", admitted
    original = await BatchStore(world.db).members(admitted.batch_id)
    assert [(m.task_id, m.source_sha) for m in original] == [(authored["task_id"], e.source)]
    newer = commit(world.origin.clone, {"later.txt": "second release\n"}, base=e.source)
    git(world.origin.clone, "push", "origin", f"{newer}:main")
    next_source = await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                         newer, e.github)
    assert next_source["task_id"] != authored["task_id"]
    assert await BatchStore(world.db).members(admitted.batch_id) == original
    e.github.runs[admitted.candidate_sha] = "success"
    e.now[0] += 61
    e.train.wake("p")
    landed = await e.train.visit(e.target)
    assert landed.state == "delivered", landed
    assert git(world.origin.url, "merge-base", "--is-ancestor", e.source, "dev") == ""
    e.github.pr_runs[newer] = "success"
    e.now[0] += 61
    e.train.wake("p")
    second = await e.train.visit(e.target)
    assert second.batch_id != admitted.batch_id and second.batch_id
    assert [m.task_id for m in await BatchStore(world.db).members(second.batch_id)] == [next_source["task_id"]]


async def test_backmerge_supersedes_only_unfrozen_sources_and_replays(world):
    e = await source_world(world)
    first = await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                    e.source, e.github)
    again = await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                    e.source, e.github)
    assert again["task_id"] == first["task_id"] and len(e.github.pulls) == 1
    assert again == first
    newer = commit(world.origin.clone, {"later.txt": "new release\n"}, base=e.source)
    git(world.origin.clone, "push", "origin", f"{newer}:main")
    await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref, newer, e.github)
    assert (await world.db.get_task(first["task_id"])).status.value == "FAILED"
    async with world.db._engine.connect() as conn:
        assert [entry["source_sha"] for entry in await backmerge_ledger(conn, "r")] == [newer]


async def test_backmerge_disabled_records_debt_without_branch_or_pr_and_git_clears_it(world):
    e = await source_world(world)
    record = await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                     e.source, e.github, enabled=False)
    assert record["kind"] == "ledger" and not e.github.pulls
    assert not git(world.origin.url, "for-each-ref", "refs/heads/aq/backmerge/")
    repo = await e.ops.repository(SimpleNamespace(repository_id="r"))
    async with world.db._engine.connect() as conn:
        with pytest.raises(PromotionRefusal, match="outstanding backmerge"):
            await _guard_backmerges(conn, e.ops, repo, "r", e.target.target_ref, e.base)
        await _guard_backmerges(conn, e.ops, repo, "r", "refs/heads/main", e.base)
        await _guard_backmerges(conn, e.ops, repo, "r", e.target.target_ref, e.source)
    git(world.origin.clone, "push", "origin", f"{e.source}:dev")
    assert await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                    e.source, e.github) is None


async def test_recorded_ledger_survives_archive_and_can_enable_authorship(world):
    e = await source_world(world)
    record = await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                     e.source, e.github, enabled=False)
    authored = await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                       e.source, e.github)
    assert authored["task_id"] == record["task_id"] and authored["pr_url"]
    assert authored["kind"] == "source" and len(e.github.pulls) == 1
    # Force administrative archival to exercise preserved completion history.
    await world.db.archive_task(record["task_id"], hold_undelivered=False)
    async with world.db._engine.connect() as conn:
        [debt] = await backmerge_ledger(conn, "r")
    assert debt["source_sha"] == e.source
    assert debt["target_ref"] == e.target.target_ref


async def test_source_must_still_descend_from_originating_head(world):
    e = await source_world(world)
    result = await e.admission.author("p", "r", "refs/heads/main", e.target.target_ref,
                                     e.source, e.github)
    e.github.pr_runs[e.source] = "success"
    opened = await e.train.visit(e.target)
    batch = await BatchStore(world.db).get(opened.batch_id)
    [member] = await BatchStore(world.db).members(batch.id)
    snapshot = await e.lane.snapshot()
    assert await e.admission.eligible(batch, member, snapshot)
    unrelated = commit(world.origin.clone, {"replacement.txt": "rewrite\n"}, base=e.base)
    git(world.origin.clone, "push", "--force", "origin", f"{unrelated}:main")
    assert not await e.admission.eligible(batch, member, await e.lane.snapshot())
    assert member.task_id == result["task_id"]


async def test_intermediate_backmerge_is_ff_only_and_contains_no_tag(promote_env):
    e = promote_env
    e.github = MultipleIntentGitHub(e)
    e.human.app = e.github
    e.github.runs[e.source] = "success"
    git(e.repo.store, "push", "origin", f"{e.source}:refs/heads/production")
    divergent = commit(e.repo.store, {"main-only.txt": "main\n"}, base=e.base)
    git(e.repo.store, "push", "origin", f"{divergent}:main")
    opened = await e.handler._request_promotion({
        "project_id": "p", "step_id": "release", "source_sha": e.source,
    }, backmerge={"origin_ref": "refs/heads/production"})
    assert opened["success"], opened
    e.checks.pull_request = lambda _: StepPullRequestGate(e.github, e.repo.binding)
    batch = await e.store.get(opened["batch_id"])
    members = await e.store.members(batch.id)
    held = await e.service.visit(batch, members, await e.snapshot())
    assert held.state == "held" and held.detail["reason"] == "backmerge_not_fast_forward"
    # An ordinary chain source containing the debt heals the diverged lower target.
    git(e.repo.store, "checkout", "--detach", e.source)
    git(e.repo.store, "merge", "--no-ff", "-m", "heal target", divergent)
    healed = git(e.repo.store, "rev-parse", "HEAD")
    git(e.repo.store, "push", "origin", f"{healed}:dev")
    e.github.runs[healed] = "success"
    # The ordinary request must be admitted while the backmerge is held.
    ordinary = await request(e)
    assert ordinary["success"] and ordinary["outcome"] == "requested", ordinary
    assert ordinary["pr_url"] != opened["pr_url"]
    git(e.repo.store, "push", "origin", f"{healed}:main")
    settled = await e.service.visit(batch, members, await e.snapshot())
    assert settled.state == "delivered" and settled.detail["reason"] == "superseded_by_route"
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")
    git(e.repo.store, "push", "--force", "origin", f"{divergent}:production")
    assert not await e.admission.eligible(batch, members)


async def test_fast_forward_backmerge_retains_step_approval_and_never_creates_tag(promote_env):
    e = promote_env
    git(e.repo.store, "push", "origin", f"{e.source}:refs/heads/production")
    opened = await e.handler._request_promotion({
        "project_id": "p", "step_id": "release", "source_sha": e.source,
    }, backmerge={"origin_ref": "refs/heads/production"})
    assert opened["success"], opened
    e.checks.pull_request = lambda _: StepPullRequestGate(e.github, e.repo.binding)
    batch = await e.store.get(opened["batch_id"])
    members = await e.store.members(batch.id)
    held = await e.service.visit(batch, members, await e.snapshot())
    assert held.state == "held" and held.detail["reason"] == "promotion_operator_approval_missing"
    approved = await e.handler._cmd_promote_approve({
        "project_id": "p", "request_id": opened["request_id"],
    })
    assert approved["success"], approved
    delivered = await e.service.visit(batch, members, await e.snapshot())
    assert delivered.state == "delivered", delivered
    assert git(e.ops.git.remote_path, "rev-parse", "main") == e.source
    assert not git(e.ops.git.remote_path, "for-each-ref", "refs/tags/")


async def test_backmerge_mechanism_observes_step_target_and_honors_disabled_flow(promote_env):
    e = promote_env
    hotfix = commit(e.repo.store, {"hotfix.txt": "fix\n"}, base=e.base)
    git(e.repo.store, "push", "origin", f"{hotfix}:main")
    flow = FlowSchema.validate([{"id": "release", "source": "dev", "target": "main",
                               "after": {"backmerge": False}}], default_branch="dev").flow
    async with e.db.immediate() as conn:
        await conn.execute(update(projects).values(promotion_flow=flow))
    result = await e.handler._cmd_integration_backmerge_source({"project_id": "p", "step_id": "release"})
    assert result["success"], result
    [debt] = result["backmerges"]
    assert debt["target_ref"] == "refs/heads/dev" and debt["source_sha"] == hotfix
    git(e.repo.store, "fetch", "origin", "dev")
    status = await e.handler._cmd_promote_status({"project_id": "p"})
    assert status["backmerges"][0]["state"] == "pending"
    git(e.repo.store, "checkout", "--detach", e.source)
    git(e.repo.store, "merge", "--no-ff", "-m", "manual backmerge", hotfix)
    git(e.repo.store, "push", "origin", "HEAD:dev")
    git(e.repo.store, "fetch", "origin", "dev")
    status = await e.handler._cmd_promote_status({"project_id": "p"})
    assert status["backmerges"][0]["state"] == "contained"
