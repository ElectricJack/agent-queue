"""A train source whose recorded base is not its ancestor never strands a batch.

Reproduces bright-nexus-97.  Batch ``integration-batch-e694…`` froze
``fresh-flare-12`` (recorded base ``f736`` on main, head ``82ae`` stacked on
``5b83``, a line that never contained ``f736``) as member 0 beside the valid
``keen-beacon-16``.  Construction returned ``source_moved`` after activating
repair stage 0, the root-train graph ended its run ``failed``, and the batch sat
``building`` with no delegate while the scheduler renewed its lease.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_candidate_revisions,
    integration_outbox,
    integration_repair_operations,
    integration_repair_stages,
    integration_review_evidence,
    integration_source_ci,
    messages,
    playbook_artifacts,
    project_integration_leases,
    project_integration_schedules,
    projects,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.git.github_app import GitHubRepositoryBinding
from src.git.manager import GitManager
from src.integration.candidates import CONSTRUCTION_RETRY_SECONDS, CandidateService
from src.integration.github_review_poll import GitHubReviewPoller
from src.integration.promotion import PromotionService
from src.integration.repair import CONSTRUCTION_REDRIVE_GRACE_SECONDS, RepairService
from src.integration.review_evidence import ReviewEvidenceProducer
from src.integration.scheduler import IntegrationScheduler, TrainService
from src.integration.source_ancestry import (
    ANCESTRY_DECISION_PATH,
    ANCESTRY_REVIEWER,
    SourceAncestryInvalid,
)
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, TaskStatus
from tests.db_fixtures import lease_dsn
from tests.test_integration_candidates import (
    _AppClient,
    _artifact,
    _AuditForge,
    _LocalPushGit,
    _policy,
    _scrubbed_env,
)
from tests.test_integration_review_evidence import review_case  # noqa: F401 (fixture)

FRESH, KEEN = "fresh-flare-12", "keen-beacon-16"
REVIEWED_ARTIFACTS = Path(__file__).resolve().parents[1] / "src" / "prompts" / "reviewed_playbooks"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env=_scrubbed_env(),
    ).stdout.strip()


def _commit(work: Path, name: str, text: str, message: str) -> str:
    (work / name).write_text(text)
    _git(work, "add", name)
    _git(work, "commit", "-m", message)
    return _git(work, "rev-parse", "HEAD")


def _graph(tmp_path: Path) -> dict[str, str | Path]:
    """The incident's shape: the stacked head never contained its recorded base."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "config", "user.name", "Ancestry Test")
    _git(work, "config", "user.email", "ancestry@example.test")
    fork = _commit(work, "base.txt", "base\n", "fork point (82b19 role)")
    _git(work, "push", "origin", "main")
    _git(work, "switch", "-c", "aq/stacked", fork)
    stacked = _commit(work, "stacked.txt", "other work\n", "stacked line (5b83 role)")
    _git(work, "push", "origin", "HEAD:refs/heads/aq/stacked")
    _git(work, "switch", "main")
    recorded = _commit(work, "main.txt", "recorded base\n", "recorded base (f736 role)")
    _git(work, "push", "origin", "main")
    _git(work, "switch", "-c", f"aq/{FRESH}", stacked)
    fresh = _commit(work, "fresh.txt", "fresh\n", "fresh-flare-12 head (82ae role)")
    _git(work, "push", "origin", f"HEAD:refs/heads/aq/{FRESH}")
    _git(work, "switch", "-c", f"aq/{KEEN}", recorded)
    keen = _commit(work, "keen.txt", "keen\n", "keen-beacon-16 head")
    _git(work, "push", "origin", f"HEAD:refs/heads/aq/{KEEN}")
    _git(work, "switch", "main")
    observed = _commit(work, "later.txt", "later\n", "observed main (290b role)")
    _git(work, "push", "origin", "main")
    return {
        "origin": origin, "work": work, "fork": fork, "stacked": stacked,
        "recorded": recorded, "fresh": fresh, "keen": keen, "observed": observed,
        "fresh_tree": _git(work, "rev-parse", f"{fresh}^{{tree}}"),
        "keen_tree": _git(work, "rev-parse", f"{keen}^{{tree}}"),
    }


def _continuous_policy(*, source_ci: bool = True) -> dict:
    policy = _policy()
    policy["root"]["repair"].update(
        conflict_scope="batch", on_exhausted="continue", source_ci=source_ci
    )
    policy["root"]["admission"] = "authorized"
    return policy


@pytest.fixture
async def train(tmp_path, reuse_database):
    graph = _graph(tmp_path)
    db = await reuse_database("source-ancestry.db")
    await db.create_profile(AgentProfile(id="repairer", name="Repairer"))
    await db.create_profile(AgentProfile(id="debugger", name="Debugger"))
    await db.create_project(Project(id="p", name="Source ancestry"))
    await db.create_repo(RepoConfig(
        id="repo", project_id="p", source_type=RepoSourceType.CLONE,
        url=str(graph["origin"]), default_branch="main",
    ))
    await db.update_project(
        "p", hierarchical_integration_mode="train", integration_repository_id="repo",
        hierarchical_integration_policy=_continuous_policy(), integration_mode="pull_request",
    )
    async with db.immediate() as conn:
        # Live train projects carry their desired mode; a catch-up needs it.
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_desired_mode="train"))
        await conn.execute(insert(playbook_artifacts).values(
            **_artifact().model_dump(), scope="project", scope_identifier="p",
            profile_fingerprint="", path="/tmp/source-ancestry-artifact", size_bytes=1,
            validation="{}", created_at=1.0,
        ))
        for number, (task_id, head) in enumerate(
            ((FRESH, graph["fresh"]), (KEEN, graph["keen"])), start=1
        ):
            await conn.execute(insert(tasks).values(
                id=task_id, project_id="p", repo_id="repo", title=task_id, description="work",
                status="COMPLETED", task_type="bugfix", branch_name=f"aq/{task_id}",
                pr_url=f"https://github.com/example/repo/pull/{number}",
                created_at=1.0, updated_at=1.0,
            ))
            await conn.execute(insert(task_branch_origins).values(
                id=f"origin-{task_id}", task_id=task_id, repository_id="repo",
                base_sha=graph["recorded"], creation_generation=0, reserved=True, created_at=1.0,
            ))
            await conn.execute(insert(task_integration_checkpoints).values(
                task_id=task_id, repository_id="repo", branch=f"aq/{task_id}",
                checkpoint_sha=head, generation=0, updated_at=1.0,
            ))
            await _green(conn, task_id, graph["recorded"], head)
    await IntegrationScheduler(db, clock=lambda: 0.0).configure(
        project_id="p", now=0.0, enabled=True, interval_seconds=300
    )
    yield {"db": db, "tmp_path": tmp_path, **graph}


async def _green(conn, task_id, base, head, generation=0):
    await conn.execute(insert(integration_source_ci).values(
        task_id=task_id, repository_id="repo", source_base=base, source_head=head,
        generation=generation, policy_generation=0, state="green", evidence={},
        observed_at=1000.0,
    ))


async def _approve_as_before(db, task_id, base, head, tree):
    """Approved evidence as the pre-fix admission recorded it, without an ancestry proof."""
    async with db.immediate() as conn:
        await conn.execute(insert(integration_review_evidence).values(
            id=f"review-{task_id}", source_task_id=task_id, repository_id="repo",
            source_base=base, reviewed_head_sha=head, reviewed_tree_sha=tree,
            reviewer_identity="github:human", review_kind="leaf", generation=0,
            verdict="approved",
            evidence={"decision_path": "github_pull_request", "summary": "", "feedback": ""},
            created_at=1000.0,
        ))


async def _seal(db, now: float, request_id: str | None = None) -> dict:
    if request_id is None:
        due = await IntegrationScheduler(db, clock=lambda: now).mark_due("p", now, "manual")
        assert due["outcome"] == "due"
        request_id = due["request_id"]
    sealed = await TrainService(db).seal("p", request_id, now)
    assert sealed["outcome"] == "sealed", sealed
    return sealed


def _candidate(case, now: float, *, head_ref=True) -> CandidateService:
    app = _AppClient(case["origin"] if head_ref else None)
    app.repository = GitHubRepositoryBinding(repository_id=9, full_name="example/repo")
    return CandidateService(
        case["db"], data_dir=case["tmp_path"] / "data",
        git_manager=_LocalPushGit(case["origin"]), forge_provider=_AuditForge(),
        app_client=app, clock=lambda: now,
    )


async def _rows(db, table, *conditions, order_by=None):
    statement = select(table).where(*conditions)
    if order_by is not None:
        statement = statement.order_by(order_by)
    async with db._engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(statement)).mappings()]


async def _one(db, table, *conditions):
    rows = await _rows(db, table, *conditions)
    assert len(rows) == 1, rows
    return rows[0]


async def _latest_evidence(db, task_id, head):
    rows = await _rows(
        db, integration_review_evidence,
        integration_review_evidence.c.source_task_id == task_id,
        integration_review_evidence.c.reviewed_head_sha == head,
        order_by=integration_review_evidence.c.created_at.desc(),
    )
    return rows[0] if rows else None


async def _sealed_events(db, prefix: str):
    rows = await _rows(db, integration_outbox, integration_outbox.c.event_type == "integration.sealed")
    return [row for row in rows if row["id"].startswith(prefix)]


def test_reviewed_root_train_still_ends_moved_construction_failed():
    """The graph gives these outcomes no continuation; the durable state must."""
    for playbook in ("root-train", "agent-queue-root-train"):
        artifact = json.loads((REVIEWED_ARTIFACTS / playbook / "artifact.json").read_text())
        transitions = artifact["steps"]["construct-and-test--build"]["transitions"]
        assert transitions["source_moved"] == "construct-and-test--failed"
        assert transitions["base_moved"] == "construct-and-test--failed"


async def test_stacked_source_is_withdrawn_and_the_valid_source_reseals_on_its_own(train):
    db = train["db"]
    await _approve_as_before(db, FRESH, train["recorded"], train["fresh"], train["fresh_tree"])
    await _approve_as_before(db, KEEN, train["recorded"], train["keen"], train["keen_tree"])
    first = await _seal(db, 1301.0)
    members = await _rows(
        db, integration_batch_members,
        integration_batch_members.c.batch_id == first["batch_id"],
        order_by=integration_batch_members.c.ordinal,
    )
    assert [m["task_id"] for m in members] == [FRESH, KEEN]
    schedule = await _one(db, project_integration_schedules)

    result = await _candidate(train, 1302.0).build(first["batch_id"])

    assert (result.outcome, result.member_ordinal, result.revision) == ("source_moved", 0, 0)
    batch = await _one(db, integration_batches, integration_batches.c.id == first["batch_id"])
    assert batch["lifecycle"] == "aborted"
    assert "is not an ancestor" in batch["human_abort_reason"]
    # The frozen manifest, its revision and the run's history stay exact.
    assert await _rows(
        db, integration_batch_members,
        integration_batch_members.c.batch_id == first["batch_id"],
        order_by=integration_batch_members.c.ordinal,
    ) == members
    revision = await _one(
        db, integration_candidate_revisions,
        integration_candidate_revisions.c.batch_id == first["batch_id"],
    )
    assert revision["state"] == "constructing" and revision["next_member_ordinal"] == 0
    operation = await _one(
        db, integration_repair_operations,
        integration_repair_operations.c.batch_id == first["batch_id"],
    )
    assert operation["state"] == "cancelled"
    stages = await _rows(
        db, integration_repair_stages,
        integration_repair_stages.c.operation_id == operation["id"],
    )
    assert [(s["ordinal"], s["state"], s["repair_task_id"]) for s in stages] == [
        (0, "cancelled", None)
    ]
    owner = await _one(
        db, integration_branch_owners,
        integration_branch_owners.c.ref == batch["integration_branch"],
    )
    assert owner["handoff_state"] == "released"
    # No lease is renewed for an ended batch; the sweep's catch-up is due now.
    assert await _rows(db, project_integration_leases) == []
    catchup = f"integration-sweep:p:{int(schedule['request_sequence']) + 1}"
    schedule = await _one(db, project_integration_schedules)
    assert schedule["outstanding_request_id"] == catchup
    assert schedule["catchup_trigger"] is None
    assert await _rows(db, integration_outbox, integration_outbox.c.id == catchup)
    assert await _sealed_events(db, "integration-construction-retry:") == []
    # One exact, attributable withdrawal; the valid source is untouched.
    rejection = await _latest_evidence(db, FRESH, train["fresh"])
    assert rejection["verdict"] == "rejected"
    assert rejection["reviewer_identity"] == ANCESTRY_REVIEWER
    assert rejection["evidence"]["decision_path"] == ANCESTRY_DECISION_PATH
    assert rejection["evidence"]["merge_base"] == train["fork"]
    assert rejection["evidence"]["detected_by"] == "construction"
    assert rejection["evidence"]["batch_id"] == first["batch_id"]
    assert f"git merge {train['recorded']}" in rejection["evidence"]["feedback"]
    assert (await _latest_evidence(db, KEEN, train["keen"]))["verdict"] == "approved"

    second = await _seal(db, 1401.0, catchup)
    assert [m["task_id"] for m in await _rows(
        db, integration_batch_members,
        integration_batch_members.c.batch_id == second["batch_id"],
    )] == [KEEN]
    built = await _candidate(train, 1402.0).build(second["batch_id"])
    assert built.outcome == "built"
    _git(train["origin"], "merge-base", "--is-ancestor", train["keen"], built.head_sha)
    _git(train["origin"], "merge-base", "--is-ancestor", train["observed"], built.head_sha)
    assert not await GitManager().ais_ancestor(str(train["origin"]), train["fresh"], built.head_sha)

    # The review poller routes the withdrawn, still-completed source to repair.
    routed = []

    async def handler(observation):
        routed.append(observation)
        return {"success": True}

    poller = GitHubReviewPoller(
        db, ReviewEvidenceProducer(db, None), None, ancestry_handler=handler
    )
    await poller._poll({"id": FRESH, "pr_url": "https://github.com/example/repo/pull/1"})
    assert len(routed) == 1
    assert routed[0].identity() == (FRESH, "repo", train["recorded"], train["fresh"], 0)
    assert (routed[0].reason, routed[0].detected_by, routed[0].batch_id) == (
        "source_base_not_ancestor", "construction", first["batch_id"]
    )


class _PullRequests:
    def __init__(self, heads: dict[str, tuple[str, str]]):
        self.heads = heads

    async def pull_request(self, url):
        sha, ref = self.heads[url]
        return {
            "state": "open",
            "head": {"sha": sha, "ref": ref, "repo": {"id": 7}},
            "base": {"ref": "main", "repo": {"id": 7}},
        }

    async def paged_list(self, _path):
        return []


class _GitHub:
    def __init__(self, client):
        self.client = client

    async def bind_github_repository(self, _url):
        return SimpleNamespace(repository_id=7, full_name="example/repo")

    def _github_client(self, _binding):
        return self.client


def _handler(case):
    from src.commands.handler import CommandHandler
    from src.config import AppConfig, DatabaseConfig, DiscordConfig
    from src.orchestrator import Orchestrator
    from src.vault import ensure_default_intelligence_classes

    data = str(case["tmp_path"] / "handler-data")
    ensure_default_intelligence_classes(data)
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        database=DatabaseConfig(url=lease_dsn("source-ancestry-handler.db")),
        data_dir=data,
    )
    orchestrator = Orchestrator(config)
    orchestrator.db = case["db"]
    return CommandHandler(orchestrator, config)


async def test_admission_refuses_the_stacked_head_and_reopens_it_for_repair(train):
    db = train["db"]
    producer = ReviewEvidenceProducer(
        db, PromotionService(db, data_dir=train["tmp_path"] / "data", git_manager=GitManager()),
        clock=lambda: 1000.0,
    )
    with pytest.raises(SourceAncestryInvalid) as refused:
        await producer.snapshot_authorized(FRESH, reviewed_sha=train["fresh"], policy_generation=0)
    assert refused.value.code == "invalid_ancestry"
    assert refused.value.observation.merge_base == train["fork"]
    with pytest.raises(SourceAncestryInvalid):
        await producer.snapshot_from_pull_request(
            FRESH, verdict="approved", reviewer_login="human", reviewed_sha=train["fresh"]
        )
    assert await _latest_evidence(db, FRESH, train["fresh"]) is None

    handler = _handler(train)
    client = _PullRequests({
        "https://github.com/example/repo/pull/1": (train["fresh"], f"aq/{FRESH}"),
        "https://github.com/example/repo/pull/2": (train["keen"], f"aq/{KEEN}"),
    })
    poller = GitHubReviewPoller(
        db, producer, _GitHub(client),
        ancestry_handler=handler.repair_integration_source_ancestry,
    )
    await poller.tick(1000.0)

    reopened = await db.get_task(FRESH)
    assert reopened.status is TaskStatus.READY
    assert f"git merge {train['recorded']}" in reopened.description
    assert "Do not rebase" in reopened.description
    rejection = await _latest_evidence(db, FRESH, train["fresh"])
    assert (rejection["verdict"], rejection["reviewer_identity"]) == ("rejected", ANCESTRY_REVIEWER)
    assert rejection["evidence"]["detected_by"] == "admission"
    keen = await _latest_evidence(db, KEEN, train["keen"])
    assert (keen["verdict"], keen["evidence"]["decision_path"]) == ("approved", "authorized_task")
    # Replay finds the source reopened and changes nothing.
    replay = await handler.repair_integration_source_ancestry(refused.value.observation)
    assert replay["outcome"] == "stale"
    assert len(await _rows(
        db, integration_review_evidence, integration_review_evidence.c.source_task_id == FRESH
    )) == 1

    # A close that brings the same head back is not reopened a second time.
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == FRESH).values(
            status="COMPLETED", pr_url="https://github.com/example/repo/pull/1"))
    await poller.tick(2000.0)
    assert (await db.get_task(FRESH)).status is TaskStatus.COMPLETED
    repeat = await _one(db, messages, messages.c.to_id == "supervisor-p")
    assert "already reopened once" in repeat["body"]
    assert rejection["id"] in repeat["body"]

    # The worker merges the recorded base, keeping every reviewed commit.
    work = train["work"]
    _git(work, "switch", f"aq/{FRESH}")
    _git(work, "merge", "--no-edit", train["recorded"])
    repaired = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", f"HEAD:refs/heads/aq/{FRESH}")
    _git(work, "merge-base", "--is-ancestor", train["fresh"], repaired)
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == FRESH).values(
            status="COMPLETED", pr_url="https://github.com/example/repo/pull/1"))
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == FRESH
        ).values(checkpoint_sha=repaired, generation=1))
        await _green(conn, FRESH, train["recorded"], repaired, generation=1)
    approved = await producer.snapshot_authorized(
        FRESH, reviewed_sha=repaired, policy_generation=0
    )
    assert approved["verdict"] == "approved" and approved["generation"] == 1
    async with db.immediate() as conn:
        eligible = await TrainService(db)._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request"
        )
    assert {(m["task_id"], m["source_head"]) for m in eligible} == {
        (FRESH, repaired), (KEEN, train["keen"])
    }


async def test_without_source_repair_authority_the_supervisor_is_told_once(train):
    db = train["db"]
    await db.update_project("p", hierarchical_integration_policy=_continuous_policy(source_ci=False))
    producer = ReviewEvidenceProducer(
        db, PromotionService(db, data_dir=train["tmp_path"] / "data", git_manager=GitManager()),
        clock=lambda: 1000.0,
    )
    with pytest.raises(SourceAncestryInvalid) as refused:
        await producer.snapshot_authorized(FRESH, reviewed_sha=train["fresh"], policy_generation=0)
    handler = _handler(train)
    first = await handler.repair_integration_source_ancestry(refused.value.observation)
    replay = await handler.repair_integration_source_ancestry(refused.value.observation)

    assert first["outcome"] == replay["outcome"] == "supervisor_notified"
    assert first["evidence_id"] == replay["evidence_id"]
    assert (await db.get_task(FRESH)).status is TaskStatus.COMPLETED
    message = await _one(db, messages, messages.c.to_id == "supervisor-p")
    assert f"aq task reopen-with-feedback --task-id {FRESH}" in message["body"]
    assert train["recorded"] in message["body"] and train["fresh"] in message["body"]
    assert len(await _rows(
        db, integration_review_evidence, integration_review_evidence.c.source_task_id == FRESH
    )) == 1


async def _strand_like_the_incident(train, monkeypatch) -> dict:
    """Build with the pre-fix construction: ``source_moved`` and nothing else."""
    db = train["db"]
    await _approve_as_before(db, FRESH, train["recorded"], train["fresh"], train["fresh_tree"])
    await _approve_as_before(db, KEEN, train["recorded"], train["keen"], train["keen_tree"])
    sealed = await _seal(db, 1301.0)

    async def no_withdrawal(*_args, **_kwargs):
        return None

    async def no_retry(*_args, **_kwargs):
        return None

    with monkeypatch.context() as patched:
        patched.setattr(CandidateService, "_withdraw_invalid_sources", no_withdrawal)
        patched.setattr(CandidateService, "_schedule_construction_retry", no_retry)
        stranded = await _candidate(train, 1302.0).build(sealed["batch_id"])
    assert (stranded.outcome, stranded.member_ordinal) == ("source_moved", 0)
    batch = await _one(db, integration_batches, integration_batches.c.id == sealed["batch_id"])
    stage = await _one(
        db, integration_repair_stages,
        integration_repair_stages.c.operation_id == sealed["operation_id"],
    )
    # The operator database's row for e694, field for field.
    assert batch["lifecycle"] == "building"
    assert (stage["ordinal"], stage["state"], stage["repair_task_id"], stage["writer_kind"]) == (
        0, "active", None, None
    )
    assert (await _one(db, integration_candidate_revisions))["state"] == "constructing"
    assert await _rows(db, project_integration_leases)
    return {**sealed, "deadline": float(stage["deadline_at"]), "attempts": stage["attempts"]}


async def test_incident_shape_is_redriven_at_its_stage_deadline_and_withdrawn(train, monkeypatch):
    db = train["db"]
    stranded = await _strand_like_the_incident(train, monkeypatch)
    operation_id, deadline = stranded["operation_id"], stranded["deadline"]
    repair = RepairService(db, clock=lambda: deadline + 1)

    early = await repair.expire(operation_id, 0, now=deadline - 1)
    assert early["outcome"] == "not_due"
    due = await repair.expire(operation_id, 0, now=deadline + 1)
    assert due["outcome"] == "not_due"
    again = await repair.expire(operation_id, 0, now=deadline + 2)
    assert again["outcome"] == "not_due"
    redrives = await _sealed_events(db, "integration-construction-redrive:")
    assert [row["payload"]["batch_id"] for row in redrives] == [stranded["batch_id"]]
    assert redrives[0]["payload"]["operation_id"] == operation_id
    stage = await _one(
        db, integration_repair_stages, integration_repair_stages.c.operation_id == operation_id
    )
    # No debug writer, and the finite budget is untouched.
    assert (stage["state"], stage["attempts"], stage["deadline_at"]) == (
        "active", stranded["attempts"], deadline
    )
    assert (await _one(
        db, integration_repair_operations, integration_repair_operations.c.id == operation_id
    ))["active_stage"] == 0

    # The playbook accepts the re-drive and runs construct-and-test again.
    result = await _candidate(train, deadline + 3).build(stranded["batch_id"])
    assert result.outcome == "source_moved"
    batch = await _one(db, integration_batches, integration_batches.c.id == stranded["batch_id"])
    assert batch["lifecycle"] == "aborted"
    assert await _rows(db, project_integration_leases) == []
    settled = await repair.expire(operation_id, 0, now=deadline + 4)
    assert (settled["outcome"], settled["action"]) == ("already_terminal", "none")


@pytest.mark.parametrize("delivered", [True, False])
async def test_unfinished_construction_escalates_after_its_redrive_grace(
    train, monkeypatch, delivered
):
    """A re-drive buys one grace period, from delivery or, if no route takes it, enqueue."""
    db = train["db"]
    stranded = await _strand_like_the_incident(train, monkeypatch)
    operation_id, deadline = stranded["operation_id"], stranded["deadline"]
    repair = RepairService(db)
    assert (await repair.expire(operation_id, 0, now=deadline + 1))["outcome"] == "not_due"
    start = deadline + 1
    if delivered:
        start = deadline + 5
        async with db.immediate() as conn:
            await conn.execute(
                update(integration_outbox)
                .where(integration_outbox.c.id.like("integration-construction-redrive:%"))
                .values(delivered_at=start)
            )
    within = await repair.expire(
        operation_id, 0, now=start + CONSTRUCTION_REDRIVE_GRACE_SECONDS - 1
    )
    assert within["outcome"] == "not_due"
    escalated = await repair.expire(
        operation_id, 0, now=start + CONSTRUCTION_REDRIVE_GRACE_SECONDS
    )
    assert (escalated["outcome"], escalated["action"]) == ("expired", "dispatch_debug")
    assert len(await _sealed_events(db, "integration-construction-redrive:")) == 1


async def test_withdrawal_waits_for_a_live_writer_and_keeps_a_continuation(train, monkeypatch):
    db = train["db"]
    stranded = await _strand_like_the_incident(train, monkeypatch)
    async with db.immediate() as conn:
        await conn.execute(insert(tasks).values(
            id="repair-writer", project_id="p", repo_id="repo", title="repair",
            description="", status="IN_PROGRESS", created_at=1.0, updated_at=1.0,
        ))
        await conn.execute(update(integration_repair_stages).where(
            integration_repair_stages.c.operation_id == stranded["operation_id"]
        ).values(repair_task_id="repair-writer", writer_kind="repair_delegate"))

    result = await _candidate(train, 1310.0).build(stranded["batch_id"])

    assert (result.outcome, result.member_ordinal) == ("source_moved", 0)
    assert "withdrawal waits on: writer" in result.reason
    batch = await _one(db, integration_batches, integration_batches.c.id == stranded["batch_id"])
    assert batch["lifecycle"] == "building"
    assert (await _one(
        db, integration_repair_operations,
        integration_repair_operations.c.id == stranded["operation_id"],
    ))["state"] == "active"
    assert await _latest_evidence(db, FRESH, train["fresh"]) is not None
    assert (await _latest_evidence(db, FRESH, train["fresh"]))["verdict"] == "approved"
    assert [row["payload"]["batch_id"] for row in await _sealed_events(
        db, "integration-construction-retry:"
    )] == [stranded["batch_id"]]


async def test_base_moved_keeps_one_pending_retry_while_the_batch_lives(train):
    db = train["db"]
    await _approve_as_before(db, KEEN, train["recorded"], train["keen"], train["keen_tree"])
    sealed = await _seal(db, 1301.0)
    now = 1320.0
    unreadable_main = _candidate(train, now, head_ref=False)

    first = await unreadable_main.build(sealed["batch_id"])
    replay = await unreadable_main.build(sealed["batch_id"])
    assert first.outcome == replay.outcome == "base_moved"
    retries = await _sealed_events(db, "integration-construction-retry:")
    window = int(now // CONSTRUCTION_RETRY_SECONDS)
    assert [row["id"] for row in retries] == [
        f"integration-construction-retry:{sealed['batch_id']}:0:{window}"
    ]
    assert retries[0]["available_at"] == now + CONSTRUCTION_RETRY_SECONDS
    assert retries[0]["payload"]["operation_id"] == sealed["operation_id"]

    # While that retry is undelivered no second one is written, even in a later
    # window; once the route accepts it, the next base_moved writes the next.
    later = _candidate(train, now + CONSTRUCTION_RETRY_SECONDS, head_ref=False)
    assert (await later.build(sealed["batch_id"])).outcome == "base_moved"
    assert len(await _sealed_events(db, "integration-construction-retry:")) == 1
    async with db.immediate() as conn:
        await conn.execute(update(integration_outbox).where(
            integration_outbox.c.id == retries[0]["id"]).values(delivered_at=now + 61))
    assert (await later.build(sealed["batch_id"])).outcome == "base_moved"
    assert len(await _sealed_events(db, "integration-construction-retry:")) == 2

    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == sealed["batch_id"]).values(lifecycle="aborted"))
    assert await later._schedule_construction_retry(sealed["batch_id"], 0) is None

    # Once main is readable again the retried build proceeds normally.
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == sealed["batch_id"]).values(lifecycle="sealed"))
    assert (await _candidate(train, now + 61.0).build(sealed["batch_id"])).outcome == "built"


async def test_reviewer_task_cannot_approve_a_train_root_that_dropped_its_base(
    review_case,  # noqa: F811 (the imported fixture)
):
    case = review_case
    db, work = case["db"], case["work"]
    _git(work, "switch", "main")
    recorded = _commit(work, "main.txt", "moved on\n", "main moves past the fork point")
    _git(work, "push", "origin", "main")
    await db.update_project("p", hierarchical_integration_mode="train")
    async with db.immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "leaf").values(parent_task_id=None))
        # A materialized origin is immutable; record the later base as a new one.
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "leaf").values(retired_at=1.5))
        await conn.execute(insert(task_branch_origins).values(
            id="origin-leaf-recorded", task_id="leaf", repository_id="repo",
            base_sha=recorded, creation_generation=2, reserved=True, created_at=2.0,
        ))
    producer = ReviewEvidenceProducer(db, case["promotion"])

    with pytest.raises(SourceAncestryInvalid) as refused:
        await producer.snapshot(
            await db.get_task("review"), case["session"], verdict="approved", summary="ok"
        )
    assert refused.value.observation.merge_base == case["base"]
    rejected = await producer.snapshot(
        await db.get_task("review"), case["session"], verdict="rejected",
        feedback="merge the recorded base",
    )
    assert rejected["verdict"] == "rejected"
