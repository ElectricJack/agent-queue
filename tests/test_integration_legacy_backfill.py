"""Git-proven backfill of pre-provenance completions (src/integration/legacy_backfill.py)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import delete, insert, select

from src.config import AppConfig
from src.database.tables import (
    branch_retirements,
    integration_legacy_deliveries,
    task_branch_origins,
)
from src.doctor.integration_checks import _BY_ID, completed_undelivered
from src.doctor.models import DoctorContext, Severity
from src.integration.branch_retirement import request_branch_retirement_on
from src.integration.delivery_branches import ROOT_DELIVERY_WAIT_SECONDS
from src.integration.legacy_backfill import (
    CONTAINED_PROOF,
    EQUIVALENT_PROOF,
    backfill_legacy_deliveries,
)
from src.integration.status import IntegrationStatusService
from src.integration.train_sources import DatabaseBatches
from src.models import Task, TaskCompletion, TaskStatus
from tests import test_integration_train_sources as _sources
from tests.test_delivery_consumers import git
from tests.test_integration_train_sources import MAIN, completed, snapshot

pytestmark = pytest.mark.asyncio
world = _sources.world  # the shared train-sources fixture


async def legacy(world, tid, *, record_commits=True, land=False, squash=False,
                 delete_branch=False, close_row=True, parent=None) -> str:
    """A completed task closed before provenance existed: no retained record."""
    db, origin = world.db, world.origin
    head = origin.work(tid)
    base = git(origin.clone, "rev-parse", f"{head}^")
    await db.create_task(Task(id=tid, project_id="p", repo_id="r", title=tid, description="",
                              branch_name=f"aq/{tid}", status=TaskStatus.IN_PROGRESS,
                              parent_task_id=parent))
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id=f"{tid}-origin", task_id=tid, repository_id="r", branch_name=f"aq/{tid}",
            parent_task_id=parent, parent_repository_id="r" if parent else None,
            parent_ref=f"aq/{parent}" if parent else None, base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=time.time(),
        ))
    if close_row:
        await db.save_task_completion(TaskCompletion(
            id=f"close-{tid}", task_id=tid, outcome="pass",
            commits=[head] if record_commits else [], completed_at=time.time(),
        ))
    await db.transition_task(tid, TaskStatus.COMPLETED)
    if land:
        origin.land(tid)
    if squash:
        git(origin.clone, "fetch", "-q", "origin")
        git(origin.clone, "checkout", "-q", "-B", "main", "origin/main")
        git(origin.clone, "merge", "-q", "--squash", f"origin/aq/{tid}")
        git(origin.clone, "commit", "-q", "-m", f"squash {tid}")
        git(origin.clone, "push", "-q", "origin", "main")
    if delete_branch:
        git(origin.clone, "push", "-q", "origin", "--delete", f"aq/{tid}")
    return head


async def blocker_codes(world):
    blockers: list[dict] = []
    # This diagnostic tests Git delivery/provenance independently of hosted PR admission.
    await DatabaseBatches(world.db).pending(
        MAIN, await snapshot(world), blockers=blockers, gate_pr=False,
    )
    return {item["task_id"]: item["code"] for item in blockers}


async def test_backfill_proves_only_git_delivered_legacy_work(world):
    db = world.db
    merged = await legacy(world, "merged", land=True)
    tip = await legacy(world, "tip", record_commits=False, land=True)
    squashed = await legacy(world, "squashed", squash=True)
    await legacy(world, "undelivered")
    await legacy(world, "gone", record_commits=False, delete_branch=True)
    await completed(world, "modern")  # retained provenance: the train answers it

    before = await blocker_codes(world)
    assert {"merged", "tip", "squashed", "undelivered", "gone"} <= set(before)
    assert set(before.values()) == {"missing_git_provenance"}

    preview = await backfill_legacy_deliveries(db, "p")
    by_task = {item["task_id"]: item for item in preview["results"]}
    assert by_task["merged"]["proof"] == CONTAINED_PROOF
    assert (by_task["merged"]["via"], by_task["merged"]["delivered_sha"]) == (
        "completion_commit", merged)
    assert (by_task["tip"]["via"], by_task["tip"]["delivered_sha"]) == ("branch_tip", tip)
    assert by_task["squashed"]["proof"] == EQUIVALENT_PROOF
    assert by_task["squashed"]["delivered_sha"] == squashed
    assert {item["task_id"] for item in preview["unproven"]} == {"undelivered", "gone"}
    assert "modern" not in by_task
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_legacy_deliveries))).all() == []

    with pytest.raises(ValueError, match="nonblank reason"):
        await backfill_legacy_deliveries(db, "p", dry_run=False)
    applied = await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op",
                                               reason="cutover backfill")
    assert applied["recorded"] == 3
    async with db._engine.connect() as conn:
        rows = {row.task_id: row for row in
                (await conn.execute(select(integration_legacy_deliveries))).all()}
    assert set(rows) == {"merged", "tip", "squashed"}
    assert rows["squashed"].proof == EQUIVALENT_PROOF
    assert "cutover backfill" in rows["tip"].reason and "branch_tip" in rows["tip"].reason

    after = await blocker_codes(world)
    assert set(after) == {"undelivered", "gone"}
    again = await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op",
                                             reason="again")
    assert again["recorded"] == 0 and again["examined"] == 2


@pytest.mark.parametrize("complete", [False, True])
async def test_backfill_content_equivalent_ignores_only_generated_conflicts(world, complete):
    db, origin = world.db, world.origin
    clone = origin.clone
    catalogue = clone / "tests" / "selection_catalogue.json"
    catalogue.parent.mkdir(exist_ok=True)
    (clone / ".gitattributes").write_text("tests/selection_catalogue.json merge=aq-generated\n")
    catalogue.write_text("base\n")
    git(clone, "add", ".")
    git(clone, "commit", "-qm", "declare generated artifact")
    git(clone, "push", "-q", "origin", "main")
    await legacy(world, "rewritten", record_commits=False)
    catalogue.write_text("source-generated\n")
    git(clone, "commit", "-qam", "source-generated")
    head = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "-q", "origin", "aq/rewritten")
    git(clone, "checkout", "-q", "main")
    if complete:
        (clone / "rewritten-work.txt").write_text("work\n")
    catalogue.write_text("target-generated\n")
    git(clone, "add", ".")
    git(clone, "commit", "-qm", "delivery with regenerated artifact")
    git(clone, "push", "-q", "origin", "main")
    preview = await backfill_legacy_deliveries(db, "p")
    if complete:
        [result] = preview["results"]
        assert (result["task_id"], result["proof"], result["via"], result["delivered_sha"]) == (
            "rewritten", EQUIVALENT_PROOF, "branch_tip", head)
        applied = await backfill_legacy_deliveries(db, "p", dry_run=False,
                                                 reason="generated equivalence regression")
        assert applied["recorded"] == 1
        async with db._engine.connect() as conn:
            assert (await conn.scalar(select(integration_legacy_deliveries.c.proof))) == (
                EQUIVALENT_PROOF)
    else:
        assert preview["results"] == []
        assert [item["task_id"] for item in preview["unproven"]] == ["rewritten"]


async def test_backfill_skips_reopened_generation(world):
    db = world.db
    await legacy(world, "merged", land=True)
    await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op", reason="r")
    # A new completion generation owes new work; the old row no longer answers it.
    await db.transition_task("merged", TaskStatus.IN_PROGRESS)
    await db.save_task_completion(TaskCompletion(
        id="close-merged-2", task_id="merged", outcome="pass", commits=[],
        completed_at=time.time() + 5,
    ))
    await db.transition_task("merged", TaskStatus.COMPLETED)
    assert "merged" in await blocker_codes(world)


async def test_backfill_records_a_legacy_close_without_a_completion_row(world):
    db = world.db
    await legacy(world, "rowless", close_row=False, land=True)
    assert "rowless" in await blocker_codes(world)
    applied = await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op",
                                               reason="r")
    assert [(r["task_id"], r["outcome"], r["via"]) for r in applied["results"]] == [
        ("rowless", "recorded", "branch_tip")]
    assert "rowless" not in await blocker_codes(world)


async def epic_with_child(world, epic, child, *, epic_status, land_child_on_epic=True):
    """A legacy epic branch carrying one child's work, closed before provenance."""
    db, origin = world.db, world.origin
    git(origin.clone, "fetch", "-q", "origin")
    git(origin.clone, "push", "-q", "origin", f"origin/main:refs/heads/aq/{epic}")
    await db.create_task(Task(id=epic, project_id="p", repo_id="r", title=epic, description="",
                              branch_name=f"aq/{epic}", status=TaskStatus.IN_PROGRESS))
    head = await legacy(world, child, parent=epic)
    if land_child_on_epic:
        git(origin.clone, "checkout", "-q", "-B", f"aq/{epic}", f"origin/aq/{epic}")
        git(origin.clone, "merge", "-q", "--no-ff", "-m", f"collect {child}", f"origin/aq/{child}")
        git(origin.clone, "push", "-q", "origin", f"aq/{epic}")
    if epic_status == TaskStatus.COMPLETED:
        await db.transition_task(epic, TaskStatus.COMPLETED)
    return head


async def test_open_epic_child_on_epic_branch_gets_retained_provenance(world):
    from src.git.manager import GitManager
    from src.integration.legacy_backfill import EPIC_PROOF
    from src.integration.provenance import CompletionIdentity, GitProvenance

    db = world.db
    head = await epic_with_child(world, "epic", "child", epic_status=TaskStatus.IN_PROGRESS)
    preview = await backfill_legacy_deliveries(db, "p")
    [item] = [r for r in preview["results"] if r["task_id"] == "child"]
    assert (item["proof"], item["delivered_sha"]) == (EPIC_PROOF, head)
    applied = await backfill_legacy_deliveries(db, "p", dry_run=False, operator_id="op",
                                               reason="r")
    assert [r["outcome"] for r in applied["results"] if r["task_id"] == "child"] == ["recorded"]
    # Provenance lives outside refs/heads; fetch its namespace explicitly.
    git(world.origin.clone, "fetch", "-q", "origin",
        "+refs/aq/provenance/*:refs/aq/provenance/*")
    record = await GitProvenance(GitManager(), str(world.origin.clone),
                                 repository_url=world.origin.url).read_completion(
        CompletionIdentity("p", "r", "child", "close-child"))
    assert record["source_oid"] == head
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_legacy_deliveries))).all() == []


async def branchless(world, tid, *, child=None):
    """A completed task with no branch of its own, optionally over one legacy child.

    Its close reported a source, so git cannot account for it and the train lists
    it as an unknown blocker for good; the NULL-branch origin keeps it an
    undelivered train input.
    """
    db, origin = world.db, world.origin
    await db.create_task(Task(id=tid, project_id="p", repo_id="r", title=tid, description="",
                              branch_name=None, status=TaskStatus.IN_PROGRESS))
    head = await legacy(world, child, parent=tid) if child else origin.work(tid)
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id=f"{tid}-origin", task_id=tid, repository_id="r", branch_name=None,
            base_sha=git(origin.clone, "rev-parse", "origin/main"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time()))
    await db.save_task_completion(TaskCompletion(
        id=f"close-{tid}", task_id=tid, outcome="pass", commits=[head], completed_at=time.time()))
    await db.transition_task(tid, TaskStatus.COMPLETED)
    return head


async def test_abandon_completed_epic_records_decision_for_epic_and_children(world):
    from src.integration.legacy_backfill import abandon_epic

    db = world.db
    await epic_with_child(world, "old", "kid", epic_status=TaskStatus.COMPLETED)
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="old-origin", task_id="old", repository_id="r", branch_name="aq/old",
            base_sha=git(world.origin.clone, "rev-parse", "origin/main"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time()))
    with pytest.raises(ValueError, match="nonblank reason"):
        await abandon_epic(db, "p", "old", dry_run=False)
    preview = await abandon_epic(db, "p", "old")
    assert {r["task_id"] for r in preview["results"]} == {"old", "kid"}
    await abandon_epic(db, "p", "old", dry_run=False, operator_id="op", reason="superseded")
    async with db._engine.connect() as conn:
        rows = {r.task_id: r.proof for r in
                (await conn.execute(select(integration_legacy_deliveries))).all()}
    assert rows == {"old": "abandoned", "kid": "abandoned"}
    async with db._engine.connect() as conn:
        retired = set((await conn.execute(select(branch_retirements.c.branch))).scalars())
    assert retired == {"aq/old", "aq/old-wip", "aq/kid", "aq/kid-wip"}
    assert not {"old", "kid"} & set(await blocker_codes(world))
    with pytest.raises(ValueError, match="completed epic"):
        await abandon_epic(db, "p", "kid")


async def test_abandon_task_records_one_decision_for_a_legacy_leaf(world):
    from src.integration.legacy_backfill import abandon_task

    db = world.db
    await legacy(world, "stranded")
    assert "stranded" in await blocker_codes(world)
    with pytest.raises(ValueError, match="nonblank reason"):
        await abandon_task(db, "p", "stranded", dry_run=False)
    preview = await abandon_task(db, "p", "stranded")
    assert [(r["task_id"], r["proof"], r["via"]) for r in preview["results"]] == [
        ("stranded", "abandoned", "operator_decision")]
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_legacy_deliveries))).all() == []
    applied = await abandon_task(db, "p", "stranded", dry_run=False, operator_id="op",
                                 reason="superseded by later work on main")
    assert [(r["task_id"], r["outcome"]) for r in applied["results"]] == [("stranded", "recorded")]
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(integration_legacy_deliveries))).one()
    assert (row.task_id, row.proof, row.delivered_sha, row.operator_id) == (
        "stranded", "abandoned", None, "op")
    assert "superseded by later work on main" in row.reason and "abandon stranded" in row.reason
    assert "stranded" not in await blocker_codes(world)
    again = await abandon_task(db, "p", "stranded", dry_run=False, operator_id="op", reason="r")
    assert again["results"] == []


async def test_abandon_task_refuses_what_git_already_proves(world):
    from src.integration.legacy_backfill import abandon_task

    db = world.db
    await legacy(world, "landed", land=True)
    await legacy(world, "squashed", squash=True)
    with pytest.raises(ValueError, match="already proves landed"):
        await abandon_task(db, "p", "landed")
    with pytest.raises(ValueError, match="already proves squashed"):
        await abandon_task(db, "p", "squashed")
    await db.create_task(Task(id="open", project_id="p", repo_id="r", title="open", description="",
                              branch_name="aq/open", status=TaskStatus.IN_PROGRESS))
    with pytest.raises(ValueError, match="only a completed task"):
        await abandon_task(db, "p", "open")
    with pytest.raises(ValueError, match="not a task of this project"):
        await abandon_task(db, "p", "no-such-task")
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_legacy_deliveries))).all() == []


async def test_a_branchless_task_is_decided_by_either_surface(world):
    from src.integration.legacy_backfill import abandon_epic, abandon_task

    db = world.db
    await branchless(world, "flat", child="under")
    await branchless(world, "loose")
    assert await blocker_codes(world) == {"flat": "missing_git_provenance",
                                          "loose": "missing_git_provenance",
                                          "under": "missing_git_provenance"}
    with pytest.raises(ValueError, match="nonblank reason"):
        await abandon_epic(db, "p", "flat", dry_run=False)
    preview = await abandon_epic(db, "p", "flat")
    assert {r["task_id"] for r in preview["results"]} == {"flat", "under"}
    await abandon_epic(db, "p", "flat", dry_run=False, operator_id="op", reason="superseded")
    leaf = await abandon_task(db, "p", "loose", dry_run=False, operator_id="op",
                              reason="superseded")
    assert [(r["task_id"], r["outcome"]) for r in leaf["results"]] == [("loose", "recorded")]
    async with db._engine.connect() as conn:
        rows = {r.task_id: r.proof for r in
                (await conn.execute(select(integration_legacy_deliveries))).all()}
    assert rows == {"flat": "abandoned", "under": "abandoned", "loose": "abandoned"}
    assert set(await blocker_codes(world)) == set()


async def test_a_container_collected_onto_main_is_refused_by_name(world):
    from src.integration.legacy_backfill import abandon_epic

    db, origin = world.db, world.origin
    base = git(origin.clone, "rev-parse", "origin/main")
    await epic_with_child(world, "landed", "part", epic_status=TaskStatus.COMPLETED)
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="landed-origin", task_id="landed", repository_id="r", branch_name="aq/landed",
            base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=time.time()))
    origin.land("landed")
    with pytest.raises(ValueError, match="already proves landed"):
        await abandon_epic(db, "p", "landed")
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_legacy_deliveries))).all() == []
    # The refusal names the alternative that works: the ordinary backfill proves it.
    preview = await backfill_legacy_deliveries(db, "p")
    assert "landed" in {r["task_id"] for r in preview["results"]}


async def test_abandon_task_on_a_container_covers_its_undelivered_children(world):
    from src.integration.legacy_backfill import abandon_task

    db = world.db
    await epic_with_child(world, "parent", "child", epic_status=TaskStatus.COMPLETED)
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="parent-origin", task_id="parent", repository_id="r", branch_name="aq/parent",
            base_sha=git(world.origin.clone, "rev-parse", "origin/main"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time()))
    applied = await abandon_task(db, "p", "parent", dry_run=False, operator_id="op",
                                 reason="superseded")
    assert [(r["task_id"], r["outcome"]) for r in applied["results"]] == [
        ("child", "recorded"), ("parent", "recorded")]
    assert not {"parent", "child"} & set(await blocker_codes(world))


async def test_backfill_never_answers_an_epic_by_its_branch(world):
    db = world.db
    # A fresh epic branch equals main before any child is collected.
    await epic_with_child(world, "fresh", "kid", epic_status=TaskStatus.COMPLETED,
                          land_child_on_epic=False)
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="fresh-origin", task_id="fresh", repository_id="r", branch_name="aq/fresh",
            base_sha=git(world.origin.clone, "rev-parse", "origin/main"),
            creation_generation=0, reserved=True, materialized=True, created_at=time.time()))
    preview = await backfill_legacy_deliveries(db, "p")
    assert "fresh" not in {r["task_id"] for r in preview["results"] + preview["unproven"]}


async def test_backfill_proves_an_epic_whose_collected_branch_is_on_main(world):
    db, origin = world.db, world.origin
    base = git(origin.clone, "rev-parse", "origin/main")
    await epic_with_child(world, "landed", "part", epic_status=TaskStatus.COMPLETED)
    async with db._engine.begin() as conn:
        await conn.execute(insert(task_branch_origins).values(
            id="landed-origin", task_id="landed", repository_id="r", branch_name="aq/landed",
            base_sha=base, creation_generation=0, reserved=True, materialized=True,
            created_at=time.time()))
    origin.land("landed")
    preview = await backfill_legacy_deliveries(db, "p")
    proven = {r["task_id"]: r["via"] for r in preview["results"]}
    assert proven["landed"] == "branch_tip" and "part" in proven


def reland(origin, tid: str) -> str:
    """Re-land *tid*'s work on main under a new, non-equivalent commit (a repair)."""
    git(origin.clone, "fetch", "-q", "origin")
    git(origin.clone, "checkout", "-q", "-B", "main", "origin/main")
    (origin.clone / f"{tid}-work.txt").write_text("work, repaired on salvage\n")
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-q", "-m", f"salvage {tid}")
    git(origin.clone, "push", "-q", "origin", "main")
    return git(origin.clone, "rev-parse", "HEAD")


async def test_relanding_records_the_landing_commit_and_retires_the_source_branch(world):
    from src.integration.delivery_branches import live_branch_references
    from src.integration.legacy_backfill import RELANDED_PROOF, record_relanding

    db = world.db
    await legacy(world, "salvaged")
    landing = reland(world.origin, "salvaged")
    # Git cannot prove a repaired re-landing: the source is neither contained
    # nor a no-op merge, so the task stays an unknown blocker forever.
    report = await backfill_legacy_deliveries(db, "p")
    assert [v["task_id"] for v in report["unproven"]] == ["salvaged"]
    assert "salvaged" in await blocker_codes(world)
    with pytest.raises(ValueError, match="nonblank reason"):
        await record_relanding(db, "p", "salvaged", landed_sha=landing, dry_run=False)
    preview = await record_relanding(db, "p", "salvaged", landed_sha=landing)
    assert [(r["task_id"], r["proof"], r["via"], r["delivered_sha"])
            for r in preview["results"]] == [
        ("salvaged", RELANDED_PROOF, "operator_relanding", landing)]
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_legacy_deliveries))).all() == []
        assert (await conn.execute(select(branch_retirements))).all() == []

    applied = await record_relanding(db, "p", "salvaged", landed_sha=landing, dry_run=False,
                                     operator_id="op", reason="salvage of the 10-07 takeover")
    assert [(r["task_id"], r["outcome"]) for r in applied["results"]] == [
        ("salvaged", "recorded")]
    async with db._engine.connect() as conn:
        row = (await conn.execute(select(integration_legacy_deliveries))).one()
        retired = (await conn.execute(select(
            branch_retirements.c.branch, branch_retirements.c.request_id,
            branch_retirements.c.task_id, branch_retirements.c.reason,
        ))).all()
        held = await live_branch_references(conn, retiring_task_id="salvaged")
    assert (row.task_id, row.proof, row.delivered_sha, row.operator_id) == (
        "salvaged", "superseded", landing, "op")
    assert f"relanded salvaged as {landing}" in row.reason
    assert "salvage of the 10-07 takeover" in row.reason
    # The source branch goes through the retirement path, and the decision
    # names the landing commit that proves it.
    assert {(r.branch, r.request_id, r.task_id) for r in retired} == {
        ("aq/salvaged", "legacy-relanded:salvaged", "salvaged"),
        ("aq/salvaged-wip", "legacy-relanded:salvaged", "salvaged"),
    }
    assert all(landing in r.reason for r in retired)
    assert "aq/salvaged" not in held
    assert "salvaged" not in await blocker_codes(world)
    with pytest.raises(ValueError, match="already has a recorded superseded delivery"):
        await record_relanding(db, "p", "salvaged", landed_sha=landing, dry_run=False,
                               operator_id="op", reason="again")


async def test_relanding_refuses_a_commit_off_the_default_branch_and_proven_work(world):
    from src.integration.legacy_backfill import record_relanding

    db = world.db
    await legacy(world, "salvaged")
    elsewhere = world.origin.work("elsewhere")
    with pytest.raises(ValueError, match="full git object id"):
        await record_relanding(db, "p", "salvaged", landed_sha="abc123")
    with pytest.raises(ValueError, match="is not on refs/heads/main"):
        await record_relanding(db, "p", "salvaged", landed_sha=elsewhere)
    with pytest.raises(ValueError, match="is not a fetched commit"):
        await record_relanding(db, "p", "salvaged", landed_sha="f" * 40)
    landing = git(world.origin.clone, "rev-parse", "origin/main")
    await legacy(world, "landed", land=True)
    with pytest.raises(ValueError, match="already proves landed"):
        await record_relanding(db, "p", "landed", landed_sha=landing)
    await db.create_task(Task(id="open", project_id="p", repo_id="r", title="open", description="",
                              branch_name="aq/open", status=TaskStatus.IN_PROGRESS))
    with pytest.raises(ValueError, match="only a completed task"):
        await record_relanding(db, "p", "open", landed_sha=landing)
    with pytest.raises(ValueError, match="not a task of this project"):
        await record_relanding(db, "p", "no-such-task", landed_sha=landing)
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_legacy_deliveries))).all() == []
        assert (await conn.execute(select(branch_retirements))).all() == []


async def test_completed_undelivered_names_only_work_nothing_will_deliver(world, tmp_path):
    """fleet-ridge-45's tail: a COMPLETED branch off main is explained or is a finding."""
    db, origin = world.db, world.origin
    await legacy(world, "landed", land=True)
    stray = await legacy(world, "stray")
    git(origin.clone, "push", "-q", "origin", "aq/stray:aq/stray-wip")
    await completed(world, "queued")  # a root whose pull request waits, completed now
    await legacy(world, "doomed")
    async with db._engine.begin() as conn:
        await request_branch_retirement_on(
            conn, request_id="test:doomed", project_id="p", repository_id="r",
            branch="aq/doomed", task_id="doomed", reason="operator abandon",
        )
    await db.create_task(Task(id="box", project_id="p", repo_id="r", title="box",
                              description="", branch_name="aq/box",
                              status=TaskStatus.IN_PROGRESS))
    await legacy(world, "kid", parent="box")
    git(origin.clone, "push", "-q", "origin", "aq/kid:aq/box")
    ctx = DoctorContext(config=AppConfig(data_dir=str(tmp_path)), db=db)

    result = await _BY_ID["integration.completed_undelivered"].run(ctx)

    assert result.severity == Severity.WARN, result.detail
    assert {(e["task_id"], e["branch"], e["wip"], e["head"])
            for e in result.data["stranded"]} == {
        ("stray", "aq/stray", False, stray), ("stray", "aq/stray-wip", True, stray)}
    assert {e["task_id"]: e["rule"] for e in result.data["accounted"]} == {
        "queued": "awaiting_delivery", "doomed": "retiring", "kid": "integrated"}
    assert "aq task archive stray --disposition deliver|obsolete|retire" in result.detail
    assert "--relanded stray=<landing-sha>" in result.detail

    # A root's pull request is a queue for a day, then a stranded root.
    later = await completed_undelivered(ctx, now=time.time() + 2 * ROOT_DELIVERY_WAIT_SECONDS)
    assert {e["task_id"]: e["rule"] for e in later["entries"] if not e["accounted"]} == {
        "stray": "unreconciled", "queued": "stranded_root"}

    # Explain (the dashboard's task panel) says the same for one task.
    service = IntegrationStatusService(db, git_first="active")
    blockers = (await service.task_blockers("stray"))["blockers"]
    assert sorted(b["ref"] for b in blockers if b["code"] == "completed_undelivered") == [
        "aq/stray", "aq/stray-wip"]
    assert "completed_undelivered" not in {
        b["code"] for b in (await service.task_blockers("queued"))["blockers"]}

    # Landing the work clears the finding (read past the observer's 30s reuse).
    origin.land("stray")
    db._delivery_observer.READ_MAX_AGE = 0.0
    settled = await _BY_ID["integration.completed_undelivered"].run(ctx)
    assert settled.severity == Severity.OK, settled.detail
    assert "3 not yet on the default branch are accounted for" in settled.detail


async def test_an_archived_branch_waits_for_the_backstop_then_is_a_finding(world, tmp_path):
    """Archived before retirement existed (2026-10-08): the daily sweep owns it, for a while."""
    db = world.db
    await legacy(world, "gone")
    assert await db.archive_task("gone", abandon_undelivered=True, abandon_reason="obsolete",
                                 disposition="obsolete")
    async with db._engine.begin() as conn:
        assert (await conn.execute(delete(branch_retirements))).rowcount
    ctx = DoctorContext(config=AppConfig(data_dir=str(tmp_path)), db=db)

    fresh = await completed_undelivered(ctx)
    assert [(e["task_id"], e["archived"], e["rule"]) for e in fresh["entries"]] == [
        ("gone", True, "awaiting_backstop")]

    [entry] = (await completed_undelivered(
        ctx, now=time.time() + 3 * ROOT_DELIVERY_WAIT_SECONDS))["entries"]
    assert (entry["rule"], entry["accounted"]) == ("unreconciled", False)
    assert "daily Git sweep" in entry["remedy"]
