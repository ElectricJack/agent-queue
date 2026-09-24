"""Legacy delivered children: observe readiness and ``adopt-legacy-deliveries``.

Real PostgreSQL and real Git: a bare ``origin.git`` and a clone the tests push
from.  A parent that finished before the train has no parent collection and
never will, so its terminal children can never get train receipts.  Status
accepts a child the development publisher delivered to the default branch;
``LegacyDeliveryAdoption`` proves the rest against the default-branch tip and
lists what it cannot prove.
"""

from __future__ import annotations

import subprocess
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, update

from src.database import Database
from src.database.tables import development_deliveries, integration_legacy_deliveries, projects
from src.git.manager import GitManager
from src.integration.controls import IntegrationControlService
from src.integration.development import DevelopmentIntegration
from src.integration.legacy_deliveries import (
    ADOPTED,
    BRANCH_TIP,
    CHILD_NOT_COMPLETED,
    DEVELOPMENT_DELIVERY,
    NO_PARENT_COLLECTION,
    NOT_ON_DEFAULT_BRANCH,
    OPERATOR_ACCEPTED,
    PARENT_NOT_TERMINAL,
    STATE_CHANGED,
    UNPROVEN,
    LegacyDeliveryAdoption,
)
from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus
from tests.db_fixtures import lease_dsn

PRINCIPAL = "supervisor session:super-p"


def git(path, *args) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


class Env:
    def __init__(self, *, db, clone, tmp_path):
        self.db, self.clone, self.tmp_path = db, clone, tmp_path

    def branch(self, name: str, *, landed: bool) -> str:
        """Push *name* with one commit; *landed* also fast-forwards main onto it."""
        git(self.clone, "checkout", "-q", "-b", name, "main")
        (self.clone / f"{name.replace('/', '-')}.txt").write_text("work\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", name)
        git(self.clone, "push", "-q", "origin", name)
        tip = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "checkout", "-q", "main")
        if landed:
            git(self.clone, "merge", "-q", "--ff-only", name)
            git(self.clone, "push", "-q", "origin", "main")
        return tip

    async def task(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        parent: str | None = None,
        branch: str | None = None,
    ) -> None:
        await self.db.create_task(
            Task(
                id=task_id,
                project_id="p",
                title=task_id,
                description="",
                parent_task_id=parent,
                repo_id="r",
                branch_name=branch,
            )
        )
        await self.db.update_task(task_id, status=status)

    async def delivery(self, delivery_id: str, *, target_ref: str, prepared_sha: str, manifest):
        now = time.time()
        async with self.db.immediate() as conn:
            await conn.execute(
                insert(development_deliveries).values(
                    id=delivery_id,
                    project_id="p",
                    repository_id="r",
                    target_ref=target_ref,
                    expected_sha=None,
                    prepared_sha=prepared_sha,
                    state="delivered",
                    manifest=manifest,
                    evidence={"kind": "local"},
                    reason="development delivery",
                    created_at=now,
                    updated_at=now,
                )
            )

    async def observe(self) -> None:
        async with self.db.immediate() as conn:
            await conn.execute(
                update(projects)
                .where(projects.c.id == "p")
                .values(
                    hierarchical_integration_mode="observe",
                    hierarchical_integration_desired_mode="observe",
                )
            )

    def adoption(self) -> LegacyDeliveryAdoption:
        development = DevelopmentIntegration(
            self.db, data_dir=self.tmp_path / "data", git=GitManager()
        )
        return LegacyDeliveryAdoption(self.db, development=development)

    async def missing_receipts(self) -> dict[str, str | None]:
        """``aq integration status``'s ``missing_receipt`` blockers, ref -> cause."""
        status = await IntegrationControlService(self.db).status("p")
        assert status["effective_mode"] == "observe"
        return {
            blocker["ref"]: blocker.get("cause")
            for blocker in status["blockers"]
            if blocker["code"] == "missing_receipt"
        }

    async def recorded(self) -> dict[str, dict]:
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(select(integration_legacy_deliveries))
            ).mappings().all()
        return {row["task_id"]: dict(row) for row in rows}


@pytest.fixture
async def env(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "config", "user.name", "Tester")
    git(clone, "config", "user.email", "tester@example.test")
    (clone / "base.txt").write_text("base\n")
    git(clone, "add", ".")
    git(clone, "commit", "-q", "-m", "base")
    git(clone, "push", "-q", "origin", "main")
    db = Database(lease_dsn("legacy-deliveries.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(
        RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(origin))
    )
    async with db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(integration_repository_id="r")
        )
    yield Env(db=db, clone=clone, tmp_path=tmp_path)
    await db.close()


async def delivered_legacy_graph(env: Env) -> dict[str, str]:
    """A parent the development publisher finished before the train existed."""
    await env.task("done", TaskStatus.DEFINED)
    # Delivered by the development publisher straight to main.
    dev_sha = env.branch("aq/done.dev", landed=True)
    await env.task("done.dev", TaskStatus.COMPLETED, parent="done", branch="aq/done.dev")
    # Landed before the development publisher existed: only its branch tip proves it.
    tip_sha = env.branch("aq/done.tip", landed=True)
    await env.task("done.tip", TaskStatus.COMPLETED, parent="done", branch="aq/done.tip")
    # Delivered into a development parent collection that later reached main;
    # the child's own branch head was rewritten and never landed.
    rewritten = env.branch("aq/done.collected", landed=False)
    collection = env.branch("aq/development/parent/done", landed=True)
    await env.task(
        "done.collected", TaskStatus.COMPLETED, parent="done", branch="aq/done.collected"
    )
    await env.db.update_task("done", status=TaskStatus.COMPLETED)
    await env.delivery(
        "delivery-main",
        target_ref="refs/heads/main",
        prepared_sha=dev_sha,
        manifest=[{"task_id": "done.dev", "source_sha": dev_sha, "parent_task_id": "done"}],
    )
    await env.delivery(
        "delivery-parent",
        target_ref="refs/heads/aq/development/parent/done",
        prepared_sha=collection,
        manifest=[
            {"task_id": "done.collected", "source_sha": rewritten, "parent_task_id": "done"}
        ],
    )
    return {"dev": dev_sha, "tip": tip_sha, "collection": collection}


async def unprovable_children(env: Env) -> None:
    """Children no proof reaches: lost work, a failed child, an open parent."""
    await env.task("gone", TaskStatus.DEFINED)
    env.branch("aq/gone.lost", landed=False)
    await env.task("gone.lost", TaskStatus.COMPLETED, parent="gone", branch="aq/gone.lost")
    await env.task("gone.failed", TaskStatus.FAILED, parent="gone")
    await env.db.update_task("gone", status=TaskStatus.COMPLETED)
    await env.task("open", TaskStatus.IN_PROGRESS)
    env.branch("aq/open.1", landed=True)
    await env.task("open.1", TaskStatus.COMPLETED, parent="open", branch="aq/open.1")


async def test_observe_status_is_clean_after_adopting_delivered_legacy_children(env):
    """The task's acceptance: no ``missing_receipt`` once the control has run."""
    shas = await delivered_legacy_graph(env)
    await env.observe()

    # The development publisher's receipt already satisfies readiness.
    assert await env.missing_receipts() == {
        "done.collected": NO_PARENT_COLLECTION,
        "done.tip": NO_PARENT_COLLECTION,
    }

    result = await env.adoption().run("p", principal=PRINCIPAL)

    assert (result["outcome"], result["count"], result["dry_run"]) == ("adopted", 2, False)
    assert result["head_sha"] == git(env.clone, "rev-parse", "origin/main")
    by_task = {item["task_id"]: item for item in result["outcomes"]}
    assert by_task["done.tip"] == {
        "task_id": "done.tip",
        "parent_task_id": "done",
        "outcome": ADOPTED,
        "proof": BRANCH_TIP,
        "delivered_sha": shas["tip"],
        "development_delivery_id": None,
    }
    assert (
        by_task["done.collected"]["proof"],
        by_task["done.collected"]["delivered_sha"],
        by_task["done.collected"]["development_delivery_id"],
    ) == (DEVELOPMENT_DELIVERY, shas["collection"], "delivery-parent")
    recorded = await env.recorded()
    assert set(recorded) == {"done.tip", "done.collected"}
    assert recorded["done.tip"]["operator_id"] == PRINCIPAL
    assert recorded["done.tip"]["target_sha"] == result["head_sha"]
    assert recorded["done.tip"]["target_ref"] == "refs/heads/main"

    assert await env.missing_receipts() == {}

    # Idempotent: nothing is left to adopt and nothing is written again.
    again = await env.adoption().run("p", principal=PRINCIPAL)
    assert (again["outcome"], again["count"], again["outcomes"]) == ("nothing_to_adopt", 0, [])
    assert await env.recorded() == recorded


async def test_dry_run_reports_every_flagged_child_and_writes_nothing(env):
    await delivered_legacy_graph(env)
    await unprovable_children(env)
    await env.observe()

    result = await env.adoption().run("p", principal=PRINCIPAL, dry_run=True)

    assert (result["outcome"], result["count"], result["dry_run"]) == ("adopted", 2, True)
    assert {
        item["task_id"]: (item["outcome"], item.get("proof") or item.get("cause"))
        for item in result["outcomes"]
    } == {
        "done.collected": (ADOPTED, DEVELOPMENT_DELIVERY),
        "done.tip": (ADOPTED, BRANCH_TIP),
        "gone.failed": (UNPROVEN, CHILD_NOT_COMPLETED),
        "gone.lost": (UNPROVEN, NOT_ON_DEFAULT_BRANCH),
        "open.1": (UNPROVEN, PARENT_NOT_TERMINAL),
    }
    lost = next(item for item in result["outcomes"] if item["task_id"] == "gone.lost")
    assert "branch aq/gone.lost at" in lost["detail"]
    assert await env.recorded() == {}
    assert set(await env.missing_receipts()) == {
        "done.collected", "done.tip", "gone.failed", "gone.lost", "open.1",
    }


async def test_unprovable_children_stay_blocked_until_explicitly_accepted(env):
    await delivered_legacy_graph(env)
    await unprovable_children(env)
    await env.observe()

    await env.adoption().run("p", principal=PRINCIPAL)
    assert await env.missing_receipts() == {
        "gone.failed": NO_PARENT_COLLECTION,
        "gone.lost": NO_PARENT_COLLECTION,
        "open.1": NO_PARENT_COLLECTION,
    }

    accepted = await env.adoption().run(
        "p",
        principal=PRINCIPAL,
        accept=["gone.lost", "gone.failed"],
        reason="work superseded; nothing to deliver",
    )

    assert (accepted["outcome"], accepted["count"]) == ("adopted", 2)
    by_task = {item["task_id"]: item for item in accepted["outcomes"]}
    assert by_task["gone.lost"]["proof"] == OPERATOR_ACCEPTED
    assert by_task["gone.lost"]["unproven_cause"] == NOT_ON_DEFAULT_BRANCH
    assert by_task["gone.failed"]["unproven_cause"] == CHILD_NOT_COMPLETED
    assert by_task["open.1"]["cause"] == PARENT_NOT_TERMINAL
    recorded = await env.recorded()
    assert recorded["gone.lost"]["delivered_sha"] is None
    assert recorded["gone.lost"]["reason"] == "work superseded; nothing to deliver"
    # An open parent's completion still needs its train receipts.
    assert await env.missing_receipts() == {"open.1": NO_PARENT_COLLECTION}


@pytest.mark.parametrize(
    ("accept", "reason", "error"),
    [
        (["open.1"], "operator decision", "parent is still open"),
        (["done.dev"], "operator decision", "does not report"),
        (["no-such-task"], "operator decision", "does not report"),
        (["gone.lost"], None, "requires an audit reason"),
    ],
)
async def test_accept_refuses_what_is_not_a_legacy_blocker(env, accept, reason, error):
    await delivered_legacy_graph(env)
    await unprovable_children(env)
    await env.observe()

    result = await env.adoption().run("p", principal=PRINCIPAL, accept=accept, reason=reason)

    assert result["outcome"] == "invalid"
    assert error in result["error"]
    assert await env.recorded() == {}


async def test_a_child_reopened_before_the_write_is_not_adopted(env):
    await delivered_legacy_graph(env)
    await env.observe()
    adoption = env.adoption()
    prove = adoption._prove

    async def reopen_then_prove(*args):
        item = await prove(*args)
        await env.db.update_task("done.tip", status=TaskStatus.READY)
        return item

    adoption._prove = reopen_then_prove
    result = await adoption.run("p", principal=PRINCIPAL)

    by_task = {item["task_id"]: item for item in result["outcomes"]}
    assert by_task["done.tip"]["outcome"] == UNPROVEN
    assert by_task["done.tip"]["cause"] == STATE_CHANGED
    assert set(await env.recorded()) == {"done.collected"}


async def test_an_undesignated_or_unknown_project_is_refused(env):
    async with env.db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(integration_repository_id=None)
        )
    result = await env.adoption().run("p", principal=PRINCIPAL)
    assert result["outcome"] == "invalid"
    assert (await env.adoption().run("missing", principal=PRINCIPAL))["outcome"] == "not_found"


async def test_status_still_flags_children_of_open_parents_and_uncollected_children(env):
    """Only a terminal parent's delivered children are settled without a collection."""
    await unprovable_children(env)
    await env.observe()
    async with env.db._engine.connect() as conn:
        count = await conn.scalar(select(func.count()).select_from(integration_legacy_deliveries))
    assert count == 0
    assert await env.missing_receipts() == {
        "gone.failed": NO_PARENT_COLLECTION,
        "gone.lost": NO_PARENT_COLLECTION,
        "open.1": NO_PARENT_COLLECTION,
    }


def test_handler_resolves_the_service_from_the_orchestrator():
    from src.integration.legacy_deliveries import legacy_delivery_adoption_for

    assert legacy_delivery_adoption_for(SimpleNamespace(db=None)) is None
    assert (
        legacy_delivery_adoption_for(
            SimpleNamespace(db=object(), orchestrator=SimpleNamespace(git=None))
        )
        is None
    )


@pytest.mark.migration
@pytest.mark.integration
async def test_upgrade_creates_the_table_on_a_database_built_before_it():
    """Only a database built before the table exercises ``a00000000022``'s create."""
    from sqlalchemy import inspect, text

    from src.database.engine import create_postgres_engine, run_schema_setup
    from src.database.tables import metadata
    from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

    if not ensure_worker_postgres_dsn():
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database("legacy_deliveries_upgrade")
    engine = create_postgres_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(text("DROP TABLE integration_legacy_deliveries"))
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await conn.execute(text("INSERT INTO alembic_version VALUES ('a00000000021')"))

        await run_schema_setup(engine)

        def _checks(sync_conn):
            return {
                c["name"]
                for c in inspect(sync_conn).get_check_constraints("integration_legacy_deliveries")
            }

        async with engine.begin() as conn:
            assert await conn.run_sync(_checks) == {
                "ck_integration_legacy_deliveries_proof",
                "ck_integration_legacy_deliveries_delivered_sha",
            }
            version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == "a00000000022"
        # Idempotent: a second pass over the upgraded database is a no-op.
        await run_schema_setup(engine)
    finally:
        await engine.dispose()
