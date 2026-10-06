"""Real Git + private PostgreSQL coverage for the development delivery path."""

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import delete, insert, select, update

from src.database import Database
from src.git.manager import GitManager
from src.database.tables import events, projects
from src.database.tables import task_dependencies, tasks
from src.integration.development import (
    DevelopmentBusy, DevelopmentPrimitives, DevelopmentPolicy,
)
from src.models import (
    Project, RepoConfig, RepoSourceType, Task, TaskCompletion, TaskStatus,
)
from tests.db_fixtures import lease_dsn


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    source = tmp_path / "source"
    git(tmp_path, "clone", str(remote), str(source))
    git(source, "config", "user.name", "Tester")
    git(source, "config", "user.email", "tester@example.test")
    (source / "base.txt").write_text("base\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    git(source, "push", "origin", "main")
    db = Database(lease_dsn("development.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Development"))
    repo = RepoConfig(id="r", project_id="p", source_type=RepoSourceType.CLONE, url=str(remote))
    await db.create_repo(repo)
    async with db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == "p").values(integration_repository_id="r")
        )
    service = DevelopmentPrimitives(db, data_dir=tmp_path / "data", git=GitManager())
    # As the daemon does: archive and task delivery readers prove
    # development delivery in git through the registered observer.
    db.set_delivery_observer(service.delivery_observer)
    await db.update_project("p", hierarchical_integration_mode="development",
                            hierarchical_integration_policy={"validation": "focused",
                                                             "commands": ["test -f base.txt"]})
    yield db, service, source, remote, repo
    await db.close()


async def _admission_blocked(setup, task_id):
    """Publication is dynamic admission, separate from graph blockedness."""
    from src.integration.admission import observe_admission

    db, service, *_ = setup
    task = await db.get_task(task_id)
    batch = await observe_admission(db, [task_id], service)
    return task.is_blocked or task_id not in batch.allowed


async def feature(setup, task_id, *, filename=None, content="new\n", retain=True):
    db, _service, source, _remote, _repo = setup
    git(source, "checkout", "-B", task_id, "main")
    (source / (filename or task_id + ".txt")).write_text(content)
    git(source, "add", ".")
    git(source, "commit", "-m", task_id)
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", task_id)
    await db.create_task(
        Task(
            id=task_id,
            project_id="p",
            repo_id="r",
            title=task_id,
            description="",
            branch_name=task_id,
            status=TaskStatus.COMPLETED,
        )
    )
    if retain:
        await complete_source(setup, task_id, "feature-" + task_id, head)
    return head


async def complete_source(setup, task_id, generation, head, *, commits=None,
                          project_id="p", repository_id=None, outcome="pass"):
    """Close *task_id* as a worker close does: the record, and its exact source in git."""
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    db, service, source, _remote, repo = setup
    await db.save_task_completion(TaskCompletion(
        id=generation, task_id=task_id, outcome=outcome,
        commits=[head] if commits is None else commits, completed_at=time.time(),
    ))
    provenance = GitProvenance(service.git, str(source), repository_url=repo.url)
    await provenance.write_completion(CompletedSource(
        CompletionIdentity(project_id, repository_id or repo.id, task_id, generation), head
    ))
    contract = await db.get_task_meta(task_id, "development_repair_sources")
    if contract and all(len(member.get("source_sha", "")) == 40 for member in contract):
        replaces = []
        for member in contract:
            completion = await db.get_task_completion(member["task_id"])
            if completion is None or completion.outcome != "pass":
                return
            binding = CompletedSource(
                CompletionIdentity("p", repo.id, member["task_id"], completion.id),
                member["source_sha"],
            )
            if await provenance.read_completion(binding.identity) is None:
                await provenance.write_completion(binding)
            replaces.append(binding)
        git(source, "fetch", "origin")
        base = git(source, "merge-base", head, "origin/" + repo.default_branch)
        await provenance.write_replacement(source_oid=head, base_oid=base, replaces=replaces,
            authority="repair_contract", reason="Exact fixture repair contract")


async def repair_cycle(setup, *, source_contract):
    db, service, source, _remote, _repo = setup
    older = "development-repair-older"
    older_sha = await feature(setup, older, filename="older.txt")
    newer = service._repair_identity([{"task_id": older, "source_sha": older_sha}])
    newer_sha = await feature(setup, newer, filename="newer.txt")
    git(source, "checkout", newer)
    (source / "older.txt").write_text("new\n")
    git(source, "add", "older.txt")
    git(source, "commit", "-m", "carry older repair content")
    newer_sha = git(source, "rev-parse", "HEAD")
    for task_id in (older, newer):
        git(source, "push", "origin", f"{task_id}:aq/{task_id}")
    async with db.immediate() as conn:
        for task_id in (older, newer):
            await conn.execute(update(tasks).where(tasks.c.id == task_id).values(
                branch_name=f"aq/{task_id}"
            ))
        await conn.execute(insert(task_dependencies).values(
            task_id=older, depends_on_task_id=newer, dep_type="blocks",
        ))
    await db.set_task_meta(newer, "development_repair_sources", [
        {"task_id": older, "source_sha": older_sha if source_contract else "invalid"}
    ])
    await complete_source(setup, older, 'older-close', older_sha)
    await complete_source(setup, newer, 'newer-close', newer_sha)
    return older, newer, older_sha, newer_sha


async def _live_parent_operation(db, parent_task_id, *, operation_id="live-parent-operation"):
    """Seed the minimal hierarchy operation that a development switch must drain."""
    from src.database.tables import integration_parent_episodes, integration_repair_operations

    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_parent_episodes).values(
                id=f"episode-{operation_id}",
                parent_task_id=parent_task_id,
                repository_id="r",
                generation=0,
                pre_collection_checkpoint_sha="a" * 40,
                created_at=1.0,
            )
        )
        await conn.execute(
            insert(integration_repair_operations).values(
                id=operation_id,
                target_kind="parent",
                parent_task_id=parent_task_id,
                episode_id=f"episode-{operation_id}",
                active_stage=0,
                state="active",
                policy_snapshot={},
                artifact_snapshot={},
                required_check_version="checks-v1",
                created_at=1.0,
                updated_at=2.0,
            )
        )


# --------------------------------------------------------------------------
# Bounded skips: a repeated identical skip ends as a stall, once.
# --------------------------------------------------------------------------


async def _lost_source_pair(setup):
    """'unpublished' lost its ref before delivery and 'dependent' waits on it.

    Neither can be published and nothing else will change that: both are
    skipped identically on every evaluation.
    """
    db, _service, source, remote, _repo = setup
    head = await feature(setup, "unpublished", retain=False)
    await db.save_task_completion(TaskCompletion(
        id="unpublished-close", task_id="unpublished", outcome="pass",
        commits=[head], completed_at=time.time(),
    ))
    git(source, "push", "origin", "--delete", "unpublished")
    git(remote, "gc", "--prune=now")
    await feature(setup, "dependent")
    await db.add_dependency("dependent", "unpublished")
    return head


async def _merge_on_main(source, *branches):
    git(source, "checkout", "main")
    git(source, "pull", "--ff-only", "origin", "main")
    for branch in branches:
        git(source, "merge", "--no-ff", "--no-edit", branch)
    git(source, "push", "origin", "main")
    return git(source, "rev-parse", "HEAD")


def _concurrent_commit(source, name):
    git(source, "checkout", "main")
    git(source, "pull", "--ff-only", "origin", "main")
    (source / name).write_text("concurrent\n")
    git(source, "add", name)
    git(source, "commit", "-m", name)
    git(source, "push", "origin", "main")
    return git(source, "rev-parse", "HEAD")


async def _assert_reassembled_over(service, db, remote, member, concurrent):
    assert git(remote, "merge-base", "--is-ancestor", concurrent, "main") == ""
    assert git(remote, "merge-base", "--is-ancestor", member, "main") == ""
    rows = [row for row in await service.rows("p") if row["target_ref"] == "refs/heads/main"]
    [moved] = [row for row in rows if row["state"] == "cancelled"]
    assert (moved["evidence"]["reason"], moved["evidence"]["observed_sha"]) == (
        "base_moved", concurrent
    )
    assert not any(row["state"] in {"parked", "publishing"} for row in rows)
    async with db._engine.connect() as conn:
        assert not (await conn.execute(select(tasks.c.id).where(
            tasks.c.id.like("development-repair-%")
        ))).scalars().all()
    return moved


async def _delivery_pending(db, task_id):
    view = await db._delivery_observer.observe([task_id])
    return not view.satisfied(task_id)


async def test_repository_exclusion_prevents_second_publisher(setup):
    db, service, _source, _remote, _repo = setup
    async with service.exclusion("r"):
        other = DevelopmentPrimitives(db, data_dir=service.data_dir, git=service.git)
        with pytest.raises(DevelopmentBusy):
            async with other.exclusion("r"):
                pytest.fail("second publisher acquired exclusion")


def test_focused_policy_requires_commands():
    with pytest.raises(ValueError, match="at least one"):
        DevelopmentPolicy().checked()


@pytest.mark.parametrize("repository", [None, "r", "other"])
async def test_development_task_explanation_uses_publisher_repository_rules(setup, repository):
    from src.integration.status import IntegrationStatusService

    db, _service, _source, _remote, _repo = setup
    await db.create_repo(RepoConfig(
        id="other", project_id="p", source_type=RepoSourceType.CLONE, url="/other.git",
    ))
    await db.create_task(Task(
        id="task", project_id="p", title="task", description="", repo_id=repository,
    ))
    result = await IntegrationStatusService(db).task_blockers("task")
    codes = {item["code"] for item in result["blockers"]}
    assert ("repository_not_designated" in codes) == (repository == "other")


async def _second_project(db, *, mode="development"):
    """Project ``web`` with its own repository, as agent-queue-web is to agent-queue."""
    await db.create_project(Project(id="web", name="Web"))
    await db.create_repo(RepoConfig(
        id="web-repo", project_id="web", source_type=RepoSourceType.CLONE, url="/web.git",
    ))
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "web")
            .values(integration_repository_id="web-repo", hierarchical_integration_mode=mode)
        )


async def _completed_elsewhere(setup, task_id, *, project_id, repo_id):
    """A completed task whose branch is on ``p``'s remote but which names *repo_id*."""
    db, _service, source, _remote, _repo = setup
    git(source, "checkout", "-B", task_id, "main")
    (source / (task_id + ".txt")).write_text("moved\n")
    git(source, "add", ".")
    git(source, "commit", "-m", task_id)
    git(source, "push", "origin", task_id)
    await db.create_task(Task(
        id=task_id, project_id=project_id, repo_id=repo_id, title=task_id, description="",
        branch_name=task_id, status=TaskStatus.COMPLETED,
    ))
    head = git(source, "rev-parse", "HEAD")
    # Closed on its source; the exact generation is retained for the project
    # and repository that deliver it (``p`` on ``r``), whatever it names now.
    await complete_source(setup, task_id, task_id + "-close", head, repository_id="r")
    return head


@pytest.mark.parametrize(
    ("destination_mode", "start_repo", "expected"),
    [
        # A repository the destination does not own gives way to the one
        # creation would have chosen there: its designated repository in a
        # delivery mode, none otherwise.
        ("development", "web-repo", "r"),
        ("disabled", "web-repo", None),
        # A repository the destination owns is a deliberate binding.
        ("development", "other", "other"),
        ("development", None, "r"),
    ],
)
async def test_project_move_binds_the_destinations_repository(
    setup, destination_mode, start_repo, expected
):
    db, _service, _source, _remote, _repo = setup
    await _second_project(db)
    await db.create_repo(RepoConfig(
        id="other", project_id="p", source_type=RepoSourceType.CLONE, url="/other.git",
    ))
    async with db.immediate() as conn:
        await conn.execute(
            update(projects)
            .where(projects.c.id == "p")
            .values(hierarchical_integration_mode=destination_mode)
        )
    await db.create_task(Task(
        id="moved", project_id="web", repo_id=start_repo, title="moved", description="",
    ))
    await db.update_task("moved", project_id="p")
    assert (await db.get_task("moved")).repo_id == expected


async def test_project_move_keeps_an_explicit_repository_and_same_project_edits(setup):
    db, _service, _source, _remote, _repo = setup
    await _second_project(db)
    await db.create_task(Task(
        id="moved", project_id="web", repo_id="web-repo", title="moved", description="",
    ))
    await db.update_task("moved", project_id="web", title="renamed")
    assert (await db.get_task("moved")).repo_id == "web-repo", "not a move"
    await db.update_task("moved", project_id="p", repo_id=None)
    assert (await db.get_task("moved")).repo_id is None, "the caller chose the repository"




async def test_repair_generation_budget_stops_recursive_dispatch(setup):
    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    previous = "original"
    for generation in range(1, 4):
        previous = await service.ensure_repair(
            "p",
            "r",
            [{"task_id": previous, "source_sha": "a" * 40}],
            "b" * 40,
            reason="validation failed",
        )
        assert (
            f"Development repair generation: {generation}"
            in (await db.get_task(previous)).description
        )
    assert (
        await service.ensure_repair(
            "p",
            "r",
            [{"task_id": previous, "source_sha": "a" * 40}],
            "b" * 40,
            reason="validation failed",
        )
        is None
    )


async def test_shared_repair_is_required_by_every_parked_source(setup):
    """A shared repair keeps provenance separate from its delivery holds."""
    db, service, _source, _remote, _repo = setup
    one = await feature(setup, "one")
    two = await feature(setup, "two")

    identity = await service.ensure_repair(
        "p",
        "r",
        [
            {"task_id": "one", "source_sha": one},
            {"task_id": "two", "source_sha": two},
        ],
        "b" * 40,
        reason="shared conflict",
    )

    repair = await db.get_task(identity)
    assert repair.parent_task_id is None
    assert set(await db.get_typed_dependencies(identity)) == {
        ("one", "discovered-from"),
        ("two", "discovered-from"),
    }
    for source_id in ("one", "two"):
        assert (identity, "blocks") in await db.get_typed_dependencies(source_id)
        assert await _admission_blocked(setup, source_id)


async def test_single_source_repair_is_a_child_without_a_reverse_cycle(setup):
    db, service, _source, _remote, _repo = setup
    source_sha = await feature(setup, "source")

    identity = await service.ensure_repair(
        "p", "r", [{"task_id": "source", "source_sha": source_sha}], "b" * 40,
        reason="single conflict",
    )

    repair = await db.get_task(identity)
    assert repair.parent_task_id == "source"
    assert ("source", "parent-child") in await db.get_typed_dependencies(identity)
    assert (identity, "blocks") not in await db.get_typed_dependencies("source")
    # The routing policy names the origin (``origins.development_repair``),
    # not the title; the repair is filed unrouted (mandatory-routing §5.3).
    assert repair.created_by_kind == "development_repair"
    assert (repair.profile_id, repair.route_source) == (None, "unrouted")


UNREPAIRED = "integration.development_conflicts_unrepaired"


async def _conflicted_source(setup, task_id="conflicted"):
    """A completed source whose change to base.txt conflicts with main, and a successor."""
    db, _service, source, _remote, _repo = setup
    original = await feature(setup, task_id, filename="base.txt", content="child\n")
    await complete_source(setup, task_id, f"{task_id}-close", original)
    await db.create_task(Task(id="next", project_id="p", title="next", description=""))
    await db.add_dependency("next", task_id)
    await _advance_main(source, "main\n")
    return original


async def _advance_main(source, content):
    git(source, "checkout", "main")
    (source / "base.txt").write_text(content)
    git(source, "commit", "-am", "advance main with a conflicting change")
    git(source, "push", "origin", "main")


async def _age_parked_rows(db):
    """Move parked operation events past the doctor's dispatch grace."""
    import json
    from src.database.tables import events

    async with db.immediate() as conn:
        rows = (await conn.execute(select(events).where(
            events.c.event_type == "development.operation"
        ))).mappings().all()
        for row in rows:
            operation = json.loads(row["payload"])
            if operation["state"] == "parked":
                operation["created_at"] -= 3600
                await conn.execute(update(events).where(events.c.id == row["id"]).values(
                    payload=json.dumps(operation)
                ))


async def _parked(service):
    return [row for row in await service.rows("p") if row["state"] == "parked"]


async def _merge_repair(setup, identity, sources, resolution):
    """Close *identity* the way its description asks: merge each exact source SHA."""
    db, _service, source, _remote, _repo = setup
    git(source, "fetch", "origin")
    git(source, "checkout", "-B", "aq/" + identity, "origin/main")
    for sha in sources:
        try:
            git(source, "merge", "--no-edit", sha)
        except subprocess.CalledProcessError:
            (source / "base.txt").write_text(resolution)
            git(source, "add", "base.txt")
            git(source, "-c", "core.editor=true", "commit", "--no-edit")
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "--force", "origin", "aq/" + identity)
    await db.transition_task(identity, TaskStatus.COMPLETED, context="test", force=True)
    await complete_source(setup, identity, identity + '-close', head)
    return head


def test_repair_chain_states_follow_the_journal():
    from src.integration.development import repair_chain

    identity = DevelopmentPrimitives._repair_identity
    source = [{"task_id": "s", "source_sha": "a" * 40}]
    first = identity(source)
    carried = [{"task_id": first, "source_sha": "b" * 40}]
    second = identity(carried)

    def row(state, manifest, target="refs/heads/main", created=1.0):
        return {"id": state + str(created), "repository_id": "r", "target_ref": target,
                "state": state, "manifest": manifest, "created_at": created}

    def chain(history, statuses, verified=()):
        return repair_chain(source, history, statuses, repository_id="r",
                            target_ref="refs/heads/main", verified=verified)

    parked = [row("parked", source)]
    assert chain(parked, {})["state"] == "missing"
    assert chain(parked, {first: "READY"})["open_repair"] == first
    assert chain(parked, {first: "COMPLETED"})["state"] == "awaiting_publication"
    assert chain(parked + [row("finished", carried, created=2.0)],
                 {first: "COMPLETED"})["state"] == "awaiting_publication"
    assert chain(parked, {first: "COMPLETED"}, verified={first})["state"] == "delivered"
    # A parent assembly ref is not the target.
    assembly = row("delivered", carried, target="refs/heads/aq/development/parent/x", created=2.0)
    assert chain(parked + [assembly], {first: "COMPLETED"})["state"] == "awaiting_publication"
    reparked = parked + [row("parked", carried, created=2.0)]
    result = chain(reparked, {first: "COMPLETED", second: "IN_PROGRESS"})
    assert (result["open_repair"], [link["task_id"] for link in result["chain"]]) == (
        second, [first, second]
    )
    assert chain(reparked, {first: "COMPLETED", second: "BLOCKED"})["state"] == "finished"
    assert chain(reparked, {first: "COMPLETED"})["state"] == "missing"


def test_carried_sources_follow_the_repair_chain():
    from src.integration.development import _carried_sources

    a, b, c = ("a" * 40, "b" * 40, "c" * 40)
    contracts = {
        "gen3": [{"task_id": "gen2", "source_sha": b}],
        "gen2": [{"task_id": "gen1", "source_sha": a}],
        "gen1": [{"task_id": "source", "source_sha": c, "parent_task_id": "epic"}],
    }
    assert _carried_sources("gen3", contracts) == {"gen2": b, "gen1": a, "source": c}
    assert _carried_sources("gen2", contracts) == {"gen1": a, "source": c}
    assert _carried_sources("unknown", contracts) == {}
    # A task reached with two revisions is unproven, and a looping journal ends.
    contracts["gen3"].append({"task_id": "gen1", "source_sha": c})
    contracts["gen1"].append({"task_id": "gen3", "source_sha": c})
    assert _carried_sources("gen3", contracts) == {
        "gen2": b, "gen1": None, "source": c, "gen3": c,
    }


def test_development_prime_omits_strict_review_protocol():
    from src.prime.sections import build_completion_protocol_section

    body = build_completion_protocol_section("example", development=True).body
    assert "no squash, PR, hosted CI, or parent verifier is required" in body
    assert "squash its" not in body
    assert "Name that PR" not in body


async def _park(service, identity, manifest, *, reason="selected validation failed"):
    """Persist a parked batch row exactly as a failed sweep would."""
    now = time.time()
    await service.save(
        {
            "id": identity,
            "project_id": "p",
            "repository_id": "r",
            "target_ref": "refs/heads/main",
            "expected_sha": None,
            "prepared_sha": None,
            "state": "parked",
            "manifest": manifest,
            "evidence": {"kind": "validation", "conclusion": "failed"},
            "reason": reason,
            "created_at": now,
            "updated_at": now,
        }
    )


async def _repair_chain(db, service, depth):
    """Return the id of a generation-*depth* repair, as the incident's chain grew."""
    previous = "original"
    for _ in range(depth):
        previous = await service.ensure_repair(
            "p", "r", [{"task_id": previous, "source_sha": "a" * 40}], "b" * 40,
            reason="validation failed",
        )
    assert f"Development repair generation: {depth}" in (await db.get_task(previous)).description
    return previous


async def test_archived_terminal_source_is_satisfied_without_a_schema_impossible_edge(setup, caplog):
    """An archived terminal source still yields a repair, minus its FK-bound edges.

    ``task_dependencies.depends_on_task_id`` references ``tasks.id``, so no
    provenance edge to an archived source can exist.  The publisher records
    that as a structured log line and carries on; it never raises.
    """
    import logging

    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    await db.update_task("original", status=TaskStatus.COMPLETED.value)
    # This models legacy data written before D2 made undelivered development
    # work unarchivable through the public archive command.
    async with db.immediate() as conn:
        task = await db._get_task_conn("original", conn=conn)
        await db._archive_one(task, conn=conn)
    assert await db.get_task("original") is None

    with caplog.at_level(logging.INFO, logger="src.integration.development"):
        identity = await service.ensure_repair(
            "p", "r", [{"task_id": "original", "source_sha": "a" * 40}], "b" * 40,
            reason="validation failed",
        )

    repair = await db.get_task(identity)
    assert repair is not None and repair.project_id == "p"
    assert repair.parent_task_id is None
    assert await db.get_typed_dependencies(identity) == []
    assert any(
        "original" in record.getMessage() and "archived" in record.getMessage()
        for record in caplog.records
    )


async def test_archived_repair_is_not_resurrected_as_a_fresh_ready_task(setup):
    """An archived repair has been filed; re-filing it loses its completion.

    ``development-repair-dfda02e25d80e0d1ab2d`` was found in ``tasks`` as READY
    and in ``archived_tasks`` as COMPLETED at the same time — one row per tick
    that read the live table alone.
    """
    db, service, _source, _remote, _repo = setup
    await feature(setup, "original")
    manifest = [{"task_id": "original", "source_sha": "a" * 40}]
    identity = await service.ensure_repair(
        "p", "r", manifest, "b" * 40, reason="validation failed"
    )
    await db.update_task(identity, status=TaskStatus.COMPLETED.value)
    # Existing archives must never be recreated as READY, even though the
    # current D2 archive guard would retain this undelivered repair.
    async with db.immediate() as conn:
        task = await db._get_task_conn(identity, conn=conn)
        await db._archive_one(task, conn=conn)
    assert await db.get_task(identity) is None

    assert (
        await service.ensure_repair("p", "r", manifest, "b" * 40, reason="validation failed")
        == identity
    )
    assert await db.get_task(identity) is None, "the archived repair stays archived"


# -- branches a delivery made obsolete (quick-ridge) -----------------------


async def aq_feature(setup, task_id, *, filename=None, content="new\n"):
    """A COMPLETED task on ``aq/<task_id>`` whose close reports its head."""
    db, _service, source, _remote, _repo = setup
    branch = "aq/" + task_id
    git(source, "checkout", "-B", branch, "main")
    (source / (filename or task_id + ".txt")).write_text(content)
    git(source, "add", ".")
    git(source, "commit", "-m", task_id)
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", branch)
    await db.create_task(
        Task(
            id=task_id, project_id="p", repo_id="r", title=task_id, description="",
            branch_name=branch, status=TaskStatus.COMPLETED,
        )
    )
    await db.save_task_completion(
        TaskCompletion(
            id=f"close-{task_id}", task_id=task_id, outcome="pass", commits=[head],
            completed_at=time.time(),
        )
    )
    await complete_source(setup, task_id, task_id + "-close", head)
    return head


def remote_branches(remote):
    branches = git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads/").split()
    return {branch for branch in branches if not branch.startswith("aq-provenance/")}


def candidate_branch(head):
    import hashlib

    return f"aq/development/{hashlib.sha256(b'p').hexdigest()[:12]}/{head}"


def assert_restorable(remote, bundle, branch, sha):
    """Restore *branch* at *sha* from *bundle* the way the deletion log says to."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        clone = Path(scratch) / "restore"
        git(Path(scratch), "clone", "-q", str(remote), str(clone))
        git(clone, "bundle", "unbundle", bundle)
        git(clone, "push", "-q", "origin", f"{sha}:refs/heads/{branch}")
    assert git(remote, "rev-parse", branch) == sha


async def journal_row(service, identity):
    return next(r for r in await service.rows("p") if r["id"] == identity)


async def test_live_branch_references_names_every_hold(setup):
    from src.database.tables import integration_branch_owners, task_branch_origins
    from src.integration.delivery_branches import live_branch_references

    db, service, _source, _remote, _repo = setup
    await db.create_task(Task(
        id="running", project_id="p", title="t", description="", branch_name="aq/running",
        status=TaskStatus.IN_PROGRESS,
    ))
    await aq_feature(setup, "undelivered")
    # Git, observed before the read, is the only delivery answer.
    delivery = await service.delivery_observer.observe({"undelivered"})
    await db.create_task(Task(
        id="development-repair-x", project_id="p", title="t", description="",
        branch_name="aq/development-repair-x", status=TaskStatus.READY,
    ))
    await db.set_task_meta(
        "development-repair-x", "development_repair_sources",
        [{"task_id": "repair-src", "source_sha": "c" * 40}],
    )
    await _live_parent_operation(db, "running")
    now = time.time()
    async with db.immediate() as conn:
        manifest = [{"task_id": "gone-src", "source_sha": "a" * 40}]
        journal = {"project_id": "p", "repository_id": "r", "manifest": manifest,
                   "evidence": {}, "reason": "t", "created_at": now, "updated_at": now}
        for operation in (
            {**journal, "id": "parked", "target_ref": "refs/heads/main", "state": "parked"},
            {**journal, "id": "cand", "state": "delivered",
             "target_ref": "refs/heads/aq/development/abc/" + "a" * 40},
        ):
            await conn.execute(service._operation_insert(**operation))
        owner = {"repository_id": "r", "owner_role": "worker", "fence_token": 0,
                 "created_at": now, "updated_at": now}
        await conn.execute(insert(integration_branch_owners).values([
            {**owner, "id": "o1", "ref": "aq/owned", "owner_id": "owned",
             "handoff_state": "attached"},
            {**owner, "id": "o2", "ref": "aq/released", "owner_id": "released",
             "handoff_state": "released"},
        ]))
        await conn.execute(insert(task_branch_origins).values(
            id="origin", task_id="discarding", repository_id="r", base_sha="b" * 40,
            branch_name="aq/epic/discarding",
            creation_generation=0, reserved=True, materialized=True, retired_at=now,
            created_at=now, discard_state="pending",
        ))
        holds = await live_branch_references(conn, delivery=delivery)
        unverified = await live_branch_references(conn)

    assert holds["aq/running"] == "task running is IN_PROGRESS"
    assert holds["aq/running-wip"] == "task running is IN_PROGRESS"
    assert holds["aq/undelivered"] == "task undelivered is not delivered yet"
    # Without git proof a completed branch is unknown, and unknown holds.
    assert unverified["aq/undelivered"] == (
        "task undelivered delivery is unknown (not verified in git)"
    )
    # A member no table resolves still holds its conventional branch.
    assert holds["aq/gone-src"] == "development batch parked is parked"
    assert holds["aq/development/abc/" + "a" * 40] == (
        "assembly carries an undelivered batch member"
    )
    assert holds["aq/repair-src"] == "open development repair development-repair-x"
    assert holds["aq/owned"] == "integration owner owned is attached"
    assert holds["aq/epic/discarding"] == "branch discard is pending"
    assert "aq/released" not in holds
    assert "main" not in holds  # never a candidate: deleters skip the default branch


# Branch-name shapes from the supervisor's 2026-09-21 cleanup
# (~/.agent-queue/backups/deleted-branches-2026-09-21.tsv): the deletion log's
# first two columns keep that file's ``branch<TAB>sha`` layout.
PRECEDENT_TASK = "aq/agile-glacier.3"
PRECEDENT_REPAIR = "aq/development-repair-011da3eb42dd37b0c591"
PRECEDENT_INTEGRATION = (
    "aq/integration/p-aeacda21cbc3fe134dede52ddb8a7a63/r-23902b8d33d997d489c44edb309aa723"
)
PRECEDENT_INTEGRATION_HELD = (
    "aq/integration/p-aeacda21cbc3fe134dede52ddb8a7a63/r-27eff558cea9166e9a0a6a3f58540a1e"
)
PRECEDENT_NON_AQ = (
    "fix/workflow-startup-policy", "worktree-harness-id-class-slice",
    "integrate/provider-usage", "gh-pages",
)


def commit_on(source, branch, base, filename, *, message=None, date=None):
    git(source, "checkout", "-B", branch, base)
    (source / filename).write_text(branch + "\n")
    git(source, "add", ".")
    git(source, "commit", "-m", message or filename, *(["--date", date] if date else []))
    return git(source, "rev-parse", "HEAD")


def stale_ctx(db, tmp_path):
    from types import SimpleNamespace

    from src.doctor.models import DoctorContext

    return DoctorContext(config=SimpleNamespace(data_dir=str(tmp_path / "doctor")), db=db)


async def seed_integration_owner(db, branch, *, state, operation_state):
    from src.database.tables import (
        integration_batches,
        integration_branch_owners,
        integration_repair_operations,
    )

    suffix = branch.rsplit("-", 1)[-1][:8]
    now = time.time()
    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(
            id=f"batch-{suffix}", project_id="p", repository_id="r", request_id=f"req-{suffix}",
            source_manifest_digest="d", lifecycle="aborted", base_sha="b" * 40,
            integration_branch=f"refs/heads/{branch}",
            policy_snapshot={}, artifact_snapshot={}, cleanup_state="complete",
            created_at=now, updated_at=now,
        ))
        await conn.execute(insert(integration_repair_operations).values(
            id=f"repair-batch-{suffix}", target_kind="batch", batch_id=f"batch-{suffix}",
            episode_id=f"batch-{suffix}", active_stage=0, state=operation_state, policy_snapshot={}, artifact_snapshot={},
            required_check_version="checks-v1", created_at=now, updated_at=now,
        ))
        await conn.execute(insert(integration_branch_owners).values(
            id=f"owner-{suffix}", repository_id="r", ref=f"refs/heads/{branch}",
            owner_id=f"repair-batch-{suffix}", owner_role="collector", fence_token=1,
            handoff_state=state, created_at=now, updated_at=now,
        ))


async def test_abandoned_work_waits_fourteen_days_like_a_failure(setup):
    from src.database.tables import task_completion_records
    from src.integration.delivery_branches import expired_task_branches

    db, _service, _source, _remote, _repo = setup
    now = time.time()
    for task_id, status in (("abandoned-meta", TaskStatus.COMPLETED),
                            ("abandoned-close", TaskStatus.FAILED),
                            ("reopened", TaskStatus.READY)):
        await db.create_task(Task(
            id=task_id, project_id="p", title=task_id, description="",
            branch_name=f"aq/{task_id}", status=status,
        ))
    await db.set_task_meta("abandoned-meta", "work_outcome", "abandoned")
    await db.set_task_meta("reopened", "work_outcome", "abandoned")
    await db.save_task_completion(TaskCompletion(
        id="close", task_id="abandoned-close", outcome="fail", completed_at=now - 20 * 86400,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(task_completion_records).where(
            task_completion_records.c.id == "close").values(work_outcome="abandoned"))
        await conn.execute(update(tasks).values(updated_at=now - 20 * 86400))
        early = await expired_task_branches(conn, now=now - 7 * 86400)
        due = await expired_task_branches(conn, now=now)

    assert early == {}
    assert due["aq/abandoned-meta"].startswith("task abandoned-meta abandoned since")
    assert due["aq/abandoned-meta-wip"] == due["aq/abandoned-meta"]
    assert due["aq/abandoned-close"].startswith("task abandoned-close abandoned since")
    assert "aq/reopened" not in due  # it can run again


async def test_an_archived_failure_expires_too(setup):
    from src.integration.delivery_branches import expired_task_branches

    db, _service, _source, _remote, _repo = setup
    await db.create_task(Task(
        id="gone", project_id="p", title="gone", description="", branch_name="aq/gone",
        status=TaskStatus.FAILED,
    ))
    async with db.immediate() as conn:
        await conn.execute(update(tasks).values(updated_at=time.time() - 15 * 86400))
    assert await db.archive_task("gone")
    async with db._engine.connect() as conn:
        due = await expired_task_branches(conn, now=time.time())
    assert due["aq/gone"].startswith("task gone (archived) FAILED since")


@pytest.mark.parametrize("state,operation_state,expected", [
    ("released", "cancelled", True),
    ("released", "completed", True),
    ("released", "active", False),
    ("reserved", "cancelled", False),
    ("attached", "completed", False),
    ("handoff_pending", "completed", False),
])
async def test_integration_refs_go_only_when_released_and_finished(
    setup, state, operation_state, expected
):
    from src.integration.delivery_branches import released_integration_refs

    db, _service, _source, _remote, _repo = setup
    await seed_integration_owner(db, PRECEDENT_INTEGRATION, state=state,
                                 operation_state=operation_state)
    async with db._engine.connect() as conn:
        released = await released_integration_refs(conn)
    assert (PRECEDENT_INTEGRATION in released) is expected


async def test_the_delete_primitive_refuses_anything_outside_aq(setup, tmp_path):
    from src.integration.delivery_branches import delete_branches

    _db, service, _source, remote, repo = setup
    store = await service.store(repo)
    main = git(remote, "rev-parse", "main")
    backups = tmp_path / "backups"
    for branch in ("main", "gh-pages", "fix/workflow-startup-policy", "trunk"):
        with pytest.raises(ValueError, match="refusing to delete"):
            await delete_branches(
                service.git, service.run_git, store, {branch: {"head": main, "reason": "t"}},
                default_branch="trunk", main_head=main, backup_dir=backups, repository_id="r",
            )
    assert not backups.exists()  # nothing logged, nothing bundled
    assert "main" in remote_branches(remote)


async def test_flow_target_in_aq_namespace_is_never_stale_or_deleted(setup, tmp_path):
    from src.database.tables import projects, repos
    from src.integration.delivery_branches import delete_branches, protected_branches

    db, service, _source, remote, repo = setup
    head = git(remote, "rev-parse", "main")
    target = "aq/production"
    git(remote, "branch", "dev", "main")
    git(remote, "branch", target, "main")
    flow = [{"id": "release", "source": "dev", "target": target}]
    async with db.immediate() as conn:
        await conn.execute(update(repos).where(repos.c.id == repo.id).values(default_branch="dev"))
        await conn.execute(update(projects).where(projects.c.id == "p").values(promotion_flow=flow))
    report = await service.stale_branches("p", delete=True)
    assert target not in {item["branch"] for item in report["stale"]}
    assert target in remote_branches(remote)
    backups = tmp_path / "protected-backups"
    store = await service.store(repo)
    with pytest.raises(ValueError, match="refusing to delete"):
        await delete_branches(
            service.git, service.run_git, store, {target: {"head": head, "reason": "landed"}},
            default_branch="dev", main_head=head, backup_dir=backups, repository_id=repo.id,
            protected=protected_branches("dev", flow),
        )
    assert not backups.exists()


# -- validation outcomes: "tests failed" is not "could not finish validating" --
#
# See tests/test_development_validation.py for the runner and classifier.



async def _repairs(db):
    return [t for t in await db.list_tasks(project_id="p") if t.id.startswith("development-repair-")]


async def truth_snapshot(setup, *, target_ref="refs/heads/main"):
    from src.integration.delivery_truth import delivery_snapshot

    _db, service, _source, _remote, repo = setup
    store = await service.store(repo, fetch=False)
    return await delivery_snapshot(
        service.git, store, project_id="p", repository_id=repo.id,
        repository_url=repo.url, target_ref=target_ref,
    )


async def test_snapshot_scopes_generation_and_fences_target_movement(setup):
    from dataclasses import replace

    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, _service, source, _remote, _repo = setup
    old_head = await feature(setup, "reopened")
    await complete_source(setup, "reopened", "old-close", old_head)
    git(source, "push", "origin", f"{old_head}:main")
    old_requests = await load_delivery_requests(
        db, ["reopened"], repository_id="r", target_ref="refs/heads/main",
    )
    snapshot = await truth_snapshot(setup)
    request = old_requests["reopened"]
    assert (await snapshot.evaluate(request)).state is DeliveryState.CONTAINED
    assert snapshot.matches([request], graph_inputs=("edge",), current_graph_inputs=("edge",))
    assert not snapshot.matches([request], graph_inputs=("edge",), current_graph_inputs=())
    assert await snapshot.is_fresh()
    assert not await snapshot.is_fresh(repository_url="wrong-repository")
    assert not await snapshot.is_fresh(target_ref="refs/heads/elsewhere")
    for wrong in (replace(request, project_id="other"), replace(request, repository_id="other"),
                  replace(request, target_ref="refs/heads/elsewhere")):
        evidence = await snapshot.evaluate(wrong)
        assert evidence.state is DeliveryState.UNKNOWN and evidence.reason == "scope_mismatch"
    git(source, "checkout", "reopened")
    (source / "new-generation.txt").write_text("new generation\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "new completion")
    newer = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", "reopened")
    await complete_source(setup, "reopened", "new-close", newer)
    current = await load_delivery_requests(
        db, ["reopened"], repository_id="r", target_ref="refs/heads/main",
    )
    fresh = await truth_snapshot(setup)
    evidence = await fresh.evaluate(current["reopened"])
    assert evidence.state is DeliveryState.PENDING and evidence.source_oid == newer
    assert not snapshot.matches(current.values())
    # The old snapshot never changes its pinned answer when main advances.
    git(source, "push", "origin", f"{newer}:main")
    assert not await snapshot.is_fresh()
    assert snapshot.target_oid == old_head
    latest = await truth_snapshot(setup)
    assert (await latest.evaluate(current["reopened"])).state is DeliveryState.CONTAINED


@pytest.mark.parametrize("prior_completion", [False, True])
async def test_delivery_uses_current_generation_before_close_record_is_saved(
    setup, prior_completion
):
    """A completed transition cannot borrow an older, delivered generation."""
    from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY
    from src.integration.delivery_truth import SETTLEMENT_KEY, DeliveryState, load_delivery_requests
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    db, service, source, _remote, repo = setup
    task_id = "closing"
    old_head = await feature(setup, task_id, retain=prior_completion)
    if prior_completion:
        git(source, "push", "origin", f"{old_head}:main")
        await db.set_task_meta(task_id, SETTLEMENT_KEY, {
            "repository_id": "r", "target_ref": "refs/heads/main",
            "completion_id": "feature-closing", "reason": "previous target",
        })
        previous = (await load_delivery_requests(
            db, [task_id], repository_id="r", target_ref="refs/heads/main",
        ))[task_id]
        assert (await (await truth_snapshot(setup)).evaluate(previous)).state is (
            DeliveryState.CONTAINED
        )

    await db.transition_task(task_id, TaskStatus.READY, context="reopen_with_feedback")
    assert await db.get_task_meta(task_id, DEVELOPMENT_COMPLETION_ID_KEY) is None
    await db.transition_task(task_id, TaskStatus.IN_PROGRESS, context="claim")
    git(source, "checkout", task_id)
    (source / "new-generation.txt").write_text("new generation\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "new completion")
    new_head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", task_id)
    new_id = "closing-new"
    await GitProvenance(service.git, str(source), repository_url=repo.url).write_completion(
        CompletedSource(CompletionIdentity("p", "r", task_id, new_id), new_head)
    )
    # This is the exact point after the terminal transition and before the
    # command has saved its descriptive TaskCompletion row.
    await db.transition_task_with_meta(
        task_id, TaskStatus.COMPLETED,
        meta={DEVELOPMENT_COMPLETION_ID_KEY: new_id}, context="session_close",
    )
    request = (await load_delivery_requests(
        db, [task_id], repository_id="r", target_ref="refs/heads/main",
    ))[task_id]
    assert request.completion_id == new_id
    assert request.completed_at is None
    assert not request.settles
    evidence = await (await truth_snapshot(setup)).evaluate(request)
    assert evidence.state is DeliveryState.PENDING
    assert evidence.source_oid == new_head

    await db.save_task_completion(TaskCompletion(
        id=new_id, task_id=task_id, outcome="pass", commits=[new_head],
        completed_at=time.time(),
    ))
    recorded = (await load_delivery_requests(
        db, [task_id], repository_id="r", target_ref="refs/heads/main",
    ))[task_id]
    assert recorded.completion_id == new_id and recorded.completed_at is not None
    await db.transition_task(task_id, TaskStatus.READY, context="reopen_with_feedback")
    assert await db.get_task_meta(task_id, DEVELOPMENT_COMPLETION_ID_KEY) is None


async def _legacy_parent_episode(db, task_id, head, *, operation=None, verified=False,
                                 completed=False):
    """An episode-only checkpoint, plus the parent-operation history named."""
    from src.database.tables import (
        integration_parent_episodes,
        integration_parent_operation_completions,
        integration_parent_verifications,
        integration_repair_operations,
        task_integration_checkpoints,
    )

    async with db._engine.begin() as conn:
        await conn.execute(insert(integration_parent_episodes).values(
            id="pre-train", parent_task_id=task_id, repository_id="r",
            generation=1, pre_collection_checkpoint_sha=head, created_at=1,
        ))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id=task_id, repository_id="r", branch=task_id,
            generation=1, checkpoint_sha=head, episode_id="pre-train", updated_at=1,
        ))
        if operation is None:
            return
        await conn.execute(insert(integration_repair_operations).values(
            id="abandoned", target_kind="parent", parent_task_id=task_id,
            episode_id="pre-train", state=operation, policy_snapshot={}, artifact_snapshot={},
            required_check_version="focused", created_at=2, updated_at=3,
        ))
        if verified:
            await conn.execute(insert(integration_parent_verifications).values(
                id="verified-once", operation_id="abandoned", parent_task_id=task_id,
                episode_id="pre-train", generation=1, head_sha=head,
                required_check_version="focused", created_at=3,
            ))
        if completed:
            await conn.execute(insert(integration_parent_operation_completions).values(
                operation_id="abandoned", verification_id="verified-once", parent_task_id=task_id,
                episode_id="pre-train", completed_at=4,
            ))


async def test_snapshot_archived_completion_survives_ref_cleanup(setup):
    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, _service, source, _remote, _repo = setup
    head = await feature(setup, "archived-source")
    await complete_source(setup, "archived-source", "archived-close", head)
    git(source, "push", "origin", f"{head}:main")
    git(source, "push", "origin", "--delete", "archived-source")
    # Archive proves the completion in git; no publication row is needed.
    await db.archive_task("archived-source")
    async with db._engine.begin() as conn:
        await conn.execute(delete(events).where(events.c.event_type == "development.operation"))
    requests = await load_delivery_requests(
        db, ["archived-source", "missing-task"], repository_id="r", target_ref="refs/heads/main",
    )
    assert "missing-task" not in requests
    request = requests["archived-source"]
    assert request.archived
    evidence = await (await truth_snapshot(setup)).evaluate(request)
    assert evidence.state is DeliveryState.CONTAINED
    assert evidence.source_oid == head and evidence.request.completion_id == "archived-close"


@pytest.mark.parametrize("failure", ["fetch", "ancestry", "invalid", "missing", "abbreviated"])
async def test_snapshot_git_errors_and_unresolved_sources_are_unknown(setup, failure):
    from dataclasses import replace

    from src.git.manager import GitError
    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, service, source, _remote, _repo = setup
    head = await feature(setup, "probe")
    await db.save_task_completion(TaskCompletion(
        id="probe-close", task_id="probe", outcome="pass", commits=[head],
        completed_at=time.time(),
    ))
    git(source, "push", "origin", f"{head}:main")
    requests = await load_delivery_requests(
        db, ["probe"], repository_id="r", target_ref="refs/heads/main",
    )
    request = requests["probe"]
    if failure == "fetch":
        with patch.object(service.git, "afetch_origin", side_effect=GitError("offline")):
            snapshot = await truth_snapshot(setup)
        assert snapshot.error and not await snapshot.is_fresh()
        evidence = await snapshot.evaluate(request)
    elif failure == "ancestry":
        snapshot = await truth_snapshot(setup)
        with patch.object(service.git, "ais_ancestor", return_value=None):
            evidence = await snapshot.evaluate(request)
    else:
        source = {"invalid": "HEAD~1", "missing": "a" * 40, "abbreviated": "abcdef123"}[failure]
        evidence = await (await truth_snapshot(setup)).evaluate(replace(request, reported_source=source))
    assert evidence.state is DeliveryState.UNKNOWN and not evidence.satisfied


async def test_unlabelled_generation_is_unknown_until_retained_in_git(setup):
    """No branch head, reported commit or retired history stands in for provenance."""
    from src.integration.delivery_truth import (
        MISSING_PROVENANCE,
        DeliveryState,
        load_delivery_requests,
    )
    from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

    db, service, source, _remote, repo = setup
    head = await feature(setup, "legacy", retain=False)
    await db.save_task_completion(TaskCompletion(
        id="legacy-close", task_id="legacy", outcome="pass", commits=[head],
        completed_at=time.time(),
    ))
    git(source, "push", "origin", f"{head}:main")
    async with db._engine.begin() as conn:
        await conn.execute(insert(events).values(
            event_type="development.legacy_provenance", project_id="p", timestamp=time.time(),
            payload=json.dumps({
                "id": "legacy-provenance:old", "legacy_id": "old", "project_id": "p",
                "repository_id": "r", "target_ref": "refs/heads/main", "created_at": time.time(),
                "manifest": [{"task_id": "legacy", "source_sha": head}],
                "completion_sources": [
                    {"task_id": "legacy", "completion_id": "legacy-close", "source_sha": head}
                ],
            }),
        ))
    request = (await load_delivery_requests(
        db, ["legacy"], repository_id="r", target_ref="refs/heads/main",
    ))["legacy"]
    snapshot = await truth_snapshot(setup)
    evidence = await snapshot.evaluate(request)
    # The branch is present, the reported commit and history name the landed
    # source, and still: the complete artifact of this generation is unknown.
    assert (evidence.state, evidence.reason) == (DeliveryState.UNKNOWN, MISSING_PROVENANCE)
    assert snapshot.unlabelled_inventory == (("legacy", "legacy-close"),)
    await GitProvenance(service.git, str(source), repository_url=repo.url).write_completion(
        CompletedSource(CompletionIdentity("p", "r", "legacy", "legacy-close"), head)
    )
    retained = await truth_snapshot(setup)
    assert (await retained.evaluate(request)).state is DeliveryState.CONTAINED
    assert retained.unlabelled_inventory == ()


async def test_snapshot_distinguishes_organization_from_branchless_code(setup):
    from dataclasses import replace

    from src.integration.delivery_truth import DeliveryState, load_delivery_requests

    db, _service, _source, _remote, _repo = setup
    await db.create_task(
        Task(
            id="organization",
            project_id="p",
            title="organization",
            description="",
            status=TaskStatus.COMPLETED,
        )
    )
    requests = await load_delivery_requests(
        db,
        ["organization"],
        repository_id="r",
        target_ref="refs/heads/main",
    )
    snapshot = await truth_snapshot(setup)
    request = requests["organization"]
    assert (await snapshot.evaluate(request)).state is DeliveryState.NO_ARTIFACT
    code = replace(request, task_id="code", has_recorded_source=True)
    assert (await snapshot.evaluate(code)).state is DeliveryState.UNKNOWN
    missing = replace(request, task_id="missing", branch_name="aq/deleted")
    assert (await snapshot.evaluate(missing)).state is DeliveryState.UNKNOWN


# -- generated artifacts: regenerated at merge, never a conflict -------------

#: A miniature scripts/regenerate-generated.sh: the inventory and catalogue are
#: pure functions of commands/ and tests/, with a count line that every added
#: command changes on both sides of a merge.
GENERATOR = """#!/bin/sh
set -e
render() {
    printf 'count: %s\\n#\\n#\\n#\\n' "$(ls "$1" | wc -l | tr -d ' ')"
    ls "$1" | LC_ALL=C sort
}
if [ "$1" = "--check" ]; then
    render commands | cmp -s - inventory.txt && render tests | cmp -s - catalogue.txt
    exit $?
fi
render commands > inventory.txt
render tests > catalogue.txt
"""
GENERATED_ATTRIBUTES = (
    "inventory.txt merge=aq-generated\n"
    "catalogue.txt merge=aq-generated\n"
    "areas.txt merge=union\n"
)
CHECK_GENERATED = "./regenerate.sh --check"


def _rendered(*names):
    return f"count: {len(names)}\n#\n#\n#\n" + "".join(f"{n}\n" for n in sorted(names))


def _regenerate(source):
    subprocess.run(["sh", "regenerate.sh"], cwd=source, check=True)


def _publish_generator(source):
    git(source, "checkout", "main")
    (source / "regenerate.sh").write_text(GENERATOR)
    (source / "regenerate.sh").chmod(0o755)
    (source / ".gitattributes").write_text(GENERATED_ATTRIBUTES)
    for directory, name in (("commands", "base"), ("tests", "test_base.py")):
        (source / directory).mkdir()
        (source / directory / name).write_text("")
    (source / "areas.txt").write_text("area:\n  - tests/test_base.py\n")
    _regenerate(source)
    git(source, "add", "-A")
    git(source, "commit", "-m", "generated artifacts")
    git(source, "push", "origin", "main")


async def command_feature(setup, task_id, *, source_edit=None):
    """A branch adding one command and its test, regenerated like a worker would."""
    db, _service, source, _remote, _repo = setup
    git(source, "checkout", "-B", task_id, "main")
    (source / "commands" / task_id).write_text("")
    (source / "tests" / f"test_{task_id}.py").write_text("")
    with (source / "areas.txt").open("a") as areas:
        areas.write(f"  - tests/test_{task_id}.py\n")
    if source_edit:
        (source / "base.txt").write_text(source_edit)
    _regenerate(source)
    git(source, "add", "-A")
    git(source, "commit", "-m", task_id)
    head = git(source, "rev-parse", "HEAD")
    git(source, "push", "origin", task_id)
    await db.create_task(Task(
        id=task_id, project_id="p", repo_id="r", title=task_id, description="",
        branch_name=task_id, status=TaskStatus.COMPLETED,
    ))
    await complete_source(setup, task_id, task_id + "-close", head)
    return head


def _batch_evidence(rows):
    return next(r for r in rows if r["reason"] == "development batch")["evidence"]


@pytest.mark.parametrize("command", ["", "  ", "'unterminated"])
def test_regenerate_policy_must_be_a_command_line(command):
    with pytest.raises(ValueError, match="regenerate"):
        DevelopmentPolicy(commands=["true"], regenerate=command).checked()


def test_development_prime_names_the_regenerate_command_only_when_configured():
    from src.prime.sections import build_completion_protocol_section

    plain = build_completion_protocol_section("example", development=True).body
    assert "hand-merge" not in plain
    body = build_completion_protocol_section(
        "example", development=True, regenerate="scripts/regenerate-generated.sh"
    ).body
    assert "Never hand-merge generated files" in body
    assert "run `scripts/regenerate-generated.sh`" in body
    assert "merge=aq-generated" in body


async def _retire_journal(db, rows):
    """Journal rows as revision a00000000038 retains them before dropping the table."""
    from importlib import import_module

    retire = import_module("migrations.versions.a00000000038_retire_development_deliveries")
    async with db._engine.begin() as conn:
        for row in rows:
            evidence, members = row["evidence"], retire._members(row["manifest"])
            if retire._outstanding(row, evidence):
                await conn.execute(DevelopmentPrimitives._operation_insert(
                    **retire._operation(row, evidence)
                ))
            await conn.execute(insert(events).values(
                event_type=retire.PROVENANCE_EVENT, project_id=row["project_id"],
                payload=json.dumps(retire._provenance(row, evidence, members)),
                timestamp=row["created_at"],
            ))


async def load_restart_request(db, repo):
    from src.integration.delivery_truth import load_delivery_requests

    return await load_delivery_requests(
        db, ["restart-source"], repository_id=repo.id, target_ref="refs/heads/main",
    )


@pytest.mark.parametrize("repairs", [3, 4, "cycle"])
async def test_legacy_repair_rows_cannot_prove_replacement_chains(setup, repairs):
    from src.integration.delivery_truth import delivery_snapshot, load_delivery_requests

    db, service, source, _remote, repo = setup
    count = 2 if repairs == "cycle" else repairs + 1
    heads = []
    for index in range(count):
        task_id = f"bounded-{index}"
        head = await feature(setup, task_id)
        heads.append(head)
        await db.save_task_completion(
            TaskCompletion(
                id=f"close-{index}",
                task_id=task_id,
                outcome="pass",
                commits=[head],
                completed_at=time.time(),
            )
        )
    if repairs != "cycle":
        git(source, "push", "origin", f"{heads[-1]}:main")
    target = git(source, "ls-remote", "origin", "refs/heads/main").split()[0]
    requests = await load_delivery_requests(
        db,
        [f"bounded-{index}" for index in range(count)],
        repository_id=repo.id,
        target_ref="refs/heads/main",
    )
    history = []
    for index in range(count if repairs == "cycle" else count - 1):
        successor = (index + 1) % count
        history.append(
            {
                "id": f"repair-row-{index}",
                "state": "adopted",
                "reason": "legacy repair",
                "expected_sha": None,
                "updated_at": time.time(),
                "project_id": "p",
                "repository_id": repo.id,
                "target_ref": "refs/heads/main",
                "created_at": time.time(),
                "prepared_sha": target,
                "manifest": [{"task_id": f"bounded-{index}", "source_sha": heads[index]}],
                "evidence": {
                    "resolved_by_delivered_repair": {
                        "task_id": f"bounded-{successor}",
                        "completion_id": f"close-{successor}",
                        "accepted_head": target,
                    }
                },
            }
        )
    # Retired repair history is kept, and still proves no replacement chain.
    await _retire_journal(db, history)
    snapshot = await delivery_snapshot(
        service.git,
        await service.store(repo, fetch=False),
        project_id="p",
        repository_id=repo.id,
        repository_url=repo.url,
        target_ref="refs/heads/main",
    )
    evidence = await snapshot.evaluate_many(requests.values())
    assert evidence["bounded-0"].satisfied is False


# --- Retargets, target-scoped parks and settling parked deliveries ----------------
# matter-engine-cpp, 2026-09-28: after main -> vg-vt-improvements, work already
# on main was parked into the new target, repairs built for main kept running,
# and sources parked on main stayed held while they applied cleanly.


def _is_ancestor(repository, commit, ref):
    return subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", commit, ref],
        capture_output=True, check=False,
    ).returncode == 0
