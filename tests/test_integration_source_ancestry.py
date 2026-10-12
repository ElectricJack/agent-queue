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
    integration_outbox,
    integration_review_evidence,
    integration_source_ci,
    messages,
    playbook_artifacts,
    projects,
    task_branch_origins,
    task_integration_checkpoints,
    tasks,
)
from src.git.manager import GitManager
from src.integration.repository_git import RepositoryGit
from src.integration.reviews import ReviewEvidenceProducer
from src.integration.source_ancestry import (
    SourceAncestryInvalid,
)
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, TaskStatus
from tests.db_fixtures import lease_dsn
from tests.test_epic_pr_review_evidence import _artifact, _policy
import os

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




async def test_without_source_repair_authority_the_supervisor_is_told_once(train):
    db = train["db"]
    await db.update_project("p", hierarchical_integration_policy=_continuous_policy(source_ci=False))
    producer = ReviewEvidenceProducer(
        db, RepositoryGit(db, data_dir=train["tmp_path"] / "data", git_manager=GitManager()),
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


@pytest.fixture(autouse=True)
def _current_root_authority(monkeypatch):
    from tests.integration_primitive_scope import authorize_root_primitives
    authorize_root_primitives(monkeypatch)

def _scrubbed_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _AMBIENT_IDENTITY_KEYS}


_AMBIENT_IDENTITY_KEYS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)
