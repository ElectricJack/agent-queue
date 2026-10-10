"""An operator authorizes one exact train root source while the train runs.

``docs/superpowers/specs/2026-10-01-explicit-root-authorization-design.md``:
the grant stands in for the policy's kind allowlist only.  It never edits the
policy, its generation, the task type or a frozen batch, and holds, gates,
rejections, admission mode and generation-pinned source CI still bind.
"""

# The imported fixture intentionally shares its name with injected test parameters.
# ruff: noqa: F811

from __future__ import annotations

import json

import pytest
from sqlalchemy import insert, select, update

from src.database.tables import (
    events,
    integration_batches,
    integration_root_authorizations,
    integration_source_ci,
    projects,
    task_branch_origins,
    task_integration_checkpoints,
    task_labels,
    tasks,
)
from src.integration.root_authorization import RootAuthorization
from src.integration.scheduler import TrainService
from tests.test_epic_pr_review_evidence import (
    _continuous_policy,
    _git,
    case,  # noqa: F401 -- pytest fixture
)


async def _chore_root(case, task_id: str, *, head: str | None = None, task_type="chore"):
    """A COMPLETED childless root whose kind the continuous policy does not admit."""
    head = head or case["second"]
    branch = f"aq/{task_id}"
    _git("push", "origin", f"{head}:refs/heads/{branch}", cwd=case["work"])
    async with case["db"].immediate() as conn:
        await conn.execute(insert(tasks).values(
            id=task_id, project_id="p", repo_id="repo", title=task_id, description="",
            status="COMPLETED", task_type=task_type, branch_name=branch,
            pr_url=f"https://github.com/o/r/pull/{task_id}",
            created_at=1.0, updated_at=1.0))
        await conn.execute(insert(task_branch_origins).values(
            id=f"origin-{task_id}", task_id=task_id, repository_id="repo",
            base_sha=case["base"], creation_generation=0, reserved=True, created_at=1.0))
        await conn.execute(insert(task_integration_checkpoints).values(
            task_id=task_id, repository_id="repo", branch=branch, checkpoint_sha=head,
            generation=0, updated_at=1.0))
    return head


async def _green(case, task_id: str, head: str, *, policy_generation: int = 0, generation=0):
    async with case["db"].immediate() as conn:
        await conn.execute(insert(integration_source_ci).values(
            task_id=task_id, repository_id="repo", source_base=case["base"], source_head=head,
            generation=generation, policy_generation=policy_generation, state="green",
            evidence={}, observed_at=1000.0))


async def _members(case) -> set[str]:
    async with case["db"].immediate() as conn:
        members = await TrainService(case["db"])._eligible_members(
            conn, project_id="p", repository_id="repo", project_mode="pull_request")
    return {member["task_id"] for member in members}


async def _project(case) -> dict:
    async with case["db"]._engine.connect() as conn:
        return dict((await conn.execute(select(projects).where(projects.c.id == "p")))
                    .mappings().one())


async def _grants(case) -> list[dict]:
    async with case["db"]._engine.connect() as conn:
        rows = (await conn.execute(select(integration_root_authorizations))).mappings().all()
    return [dict(row) for row in rows]


async def _authorized_events(case) -> list[dict]:
    async with case["db"]._engine.connect() as conn:
        rows = (await conn.execute(select(events).where(
            events.c.event_type == "integration.root_authorized"))).mappings().all()
    return [json.loads(row["payload"]) for row in rows]


def _service(case) -> RootAuthorization:
    return RootAuthorization(case["db"], clock=lambda: 2000.0)


async def _grant(case, task_id: str, head: str) -> dict:
    return await _service(case).run(
        task_id, dry_run=False, expected_head_sha=head, reason="user authorized delivery",
        operator_id="supervisor session:s",
    )


async def test_exact_grant_admits_one_root_without_touching_policy_or_active_batch(case):
    await _continuous_policy(case)
    head = await _chore_root(case, "chore-root")
    await _chore_root(case, "other-chore")
    await _green(case, "chore-root", head)
    await _green(case, "other-chore", head)
    async with case["db"].immediate() as conn:
        await conn.execute(insert(integration_batches).values(
            id="batch-active", project_id="p", repository_id="repo", request_id="req-1",
            source_manifest_digest="sha256:" + "0" * 64, base_sha=case["base"],
            lifecycle="testing", integration_branch="aq/integration/batch-active",
            policy_snapshot={"frozen": True}, artifact_snapshot={"version": 1},
            cleanup_state="none", created_at=1.0, updated_at=1.0))
        batch_before = dict((await conn.execute(select(integration_batches))).mappings().one())
    project_before = await _project(case)
    producer = case["producer"]
    assert await producer.snapshot_authorized("chore-root", reviewed_sha=head,
                                              policy_generation=0) is None
    assert await _members(case) == set()

    preview = await _service(case).run("chore-root")
    assert preview["outcome"] == "would_authorize"
    assert {key: preview[key] for key in (
        "task_type", "repository_id", "base_sha", "head_sha", "generation", "review_kind",
        "policy_generation")} == {
        "task_type": "chore", "repository_id": "repo", "base_sha": case["base"],
        "head_sha": head, "generation": 0, "review_kind": "leaf", "policy_generation": 0}
    assert await _grants(case) == []

    applied = await _grant(case, "chore-root", head)
    assert (applied["outcome"], applied["authorized_by"]) == ("authorized", "grant")
    [grant] = await _grants(case)
    assert grant["id"] == applied["authorization_id"]
    assert (grant["task_id"], grant["source_head"], grant["generation"], grant["operator_id"],
            grant["reason"]) == ("chore-root", head, 0, "supervisor session:s",
                                 "user authorized delivery")
    [event] = await _authorized_events(case)
    assert (event["authorization_id"], event["head_sha"]) == (grant["id"], head)

    # Nothing else moved: no policy edit, no generation swap, no retag, no
    # rewrite of the active batch or its frozen policy.
    assert await _project(case) == project_before
    assert (await case["db"].get_task("chore-root")).task_type.value == "chore"
    async with case["db"]._engine.connect() as conn:
        assert dict((await conn.execute(select(integration_batches))).mappings().one()) \
            == batch_before

    evidence = await producer.snapshot_authorized("chore-root", reviewed_sha=head,
                                                  policy_generation=0)
    assert evidence["evidence"]["decision_path"] == "authorized_task"
    assert evidence["evidence"]["policy_generation"] == 0
    # An unrelated root of the same kind stays refused.
    assert await producer.snapshot_authorized("other-chore", reviewed_sha=head,
                                              policy_generation=0) is None
    assert await _members(case) == {"chore-root"}


async def test_apply_is_head_fenced_and_replay_writes_nothing_new(case):
    await _continuous_policy(case)
    head = await _chore_root(case, "chore-root")
    moved = await _service(case).run(
        "chore-root", dry_run=False, expected_head_sha=case["first"], reason="r",
        operator_id="human:local-operator")
    assert moved["outcome"] == "changed"
    unreasoned = await _service(case).run(
        "chore-root", dry_run=False, expected_head_sha=head, reason=" ",
        operator_id="human:local-operator")
    assert unreasoned["outcome"] == "invalid"
    assert await _grants(case) == []

    first = await _grant(case, "chore-root", head)
    replay = await _grant(case, "chore-root", head)
    assert first["outcome"] == "authorized"
    assert (replay["outcome"], replay["authorized_by"], replay["authorization_id"]) == (
        "already_authorized", "grant", first["authorization_id"])
    assert (await _service(case).run("chore-root"))["outcome"] == "already_authorized"
    assert len(await _grants(case)) == 1
    assert len(await _authorized_events(case)) == 1


@pytest.mark.parametrize("stale", ["head", "generation"])
async def test_grant_admits_only_its_exact_source(case, stale):
    await _continuous_policy(case)
    head = await _chore_root(case, "chore-root")
    await _grant(case, "chore-root", head)
    if stale == "head":
        _git("checkout", "--detach", head, cwd=case["work"])
        (case["work"] / "later.txt").write_text("later work\n")
        _git("add", "later.txt", cwd=case["work"])
        _git("commit", "-m", "later work", cwd=case["work"])
        current = _git("rev-parse", "HEAD", cwd=case["work"])
        _git("push", "--force", "origin", "HEAD:refs/heads/aq/chore-root", cwd=case["work"])
        values = {"checkpoint_sha": current}
    else:
        current = head
        values = {"generation": 1}
    async with case["db"].immediate() as conn:
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "chore-root").values(**values))
    assert await case["producer"].snapshot_authorized(
        "chore-root", reviewed_sha=current, policy_generation=0) is None
    preview = await _service(case).run("chore-root")
    assert preview["outcome"] == "would_authorize"
    assert (preview["head_sha"], preview["generation"]) == (
        current, 1 if stale == "generation" else 0)


async def test_grant_survives_a_policy_generation_swap_but_stale_evidence_does_not(case):
    await _continuous_policy(case)
    head = await _chore_root(case, "chore-root")
    await _green(case, "chore-root", head)
    await _grant(case, "chore-root", head)
    assert await case["producer"].snapshot_authorized(
        "chore-root", reviewed_sha=head, policy_generation=0)
    assert await _members(case) == {"chore-root"}
    async with case["db"].immediate() as conn:
        assert await case["db"].cas_project_integration_control_on(
            conn, project_id="p", expected_generation=0, effective_mode="train",
            desired_mode="train", draining=False)
    # Evidence and source CI from the older generation no longer seat it.
    assert await _members(case) == set()
    assert await case["producer"].snapshot_authorized(
        "chore-root", reviewed_sha=head, policy_generation=0) is None
    # The next poll re-observes under the new generation; the grant still holds.
    evidence = await case["producer"].snapshot_authorized(
        "chore-root", reviewed_sha=head, policy_generation=1)
    assert evidence["evidence"]["policy_generation"] == 1
    async with case["db"].immediate() as conn:
        await conn.execute(update(integration_source_ci).where(
            integration_source_ci.c.task_id == "chore-root").values(policy_generation=1))
    assert await _members(case) == {"chore-root"}
    assert len(await _grants(case)) == 1


@pytest.mark.parametrize("blocker", ["hold", "gate", "rejected", "reviewed"])
async def test_holds_gates_rejections_and_reviewed_admission_still_bind(case, blocker):
    policy = await _continuous_policy(case)
    head = await _chore_root(case, "chore-root")
    await _green(case, "chore-root", head)
    await _grant(case, "chore-root", head)
    if blocker == "hold":
        async with case["db"].immediate() as conn:
            await conn.execute(insert(task_labels).values(task_id="chore-root",
                                                          label="hold:product"))
    elif blocker == "gate":
        await case["db"].create_gate("p", "human", "Product decision",
                                     waiter_task_ids=["chore-root"])
    elif blocker == "rejected":
        await case["producer"].snapshot_from_pull_request(
            "chore-root", verdict="rejected", reviewer_login="reviewer", reviewed_sha=head)
    else:
        policy["root"]["admission"] = "reviewed"
        await case["db"].update_project("p", hierarchical_integration_policy=policy)
    assert await case["producer"].snapshot_authorized(
        "chore-root", reviewed_sha=head, policy_generation=0) is None
    assert await _members(case) == set()
    # A blocked source can be neither previewed nor granted again.
    async with case["db"].immediate() as conn:
        await conn.execute(integration_root_authorizations.delete())
    assert (await _service(case).run("chore-root"))["outcome"] == "blocked"
    assert (await _grant(case, "chore-root", head))["outcome"] == "blocked"
    assert await _grants(case) == []


async def test_policy_admitted_roots_need_no_grant(case):
    policy = await _continuous_policy(case)
    async with case["db"]._engine.connect() as conn:
        source = await case["producer"]._pull_request_source_on(conn, "e1")
    result = await _grant(case, "e1", source["head"])
    assert (result["outcome"], result["authorized_by"]) == ("already_authorized", "policy_kind")
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "e1").values(task_type="chore"))
    policy["root"]["authorized_task_ids"] = ["e1"]
    await case["db"].update_project("p", hierarchical_integration_policy=policy)
    result = await _service(case).run("e1")
    assert (result["outcome"], result["authorized_by"], result["review_kind"]) == (
        "already_authorized", "policy_allowlist", "parent")
    assert await _grants(case) == []


async def test_missing_and_ineligible_roots_are_refused(case):
    await _continuous_policy(case)
    assert (await _service(case).run("missing"))["outcome"] == "not_found"
    assert (await _grant(case, "missing", case["second"]))["outcome"] == "not_found"
    await _chore_root(case, "chore-root")
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "chore-root")
                           .values(status="IN_PROGRESS"))
    assert (await _service(case).run("chore-root"))["outcome"] == "not_eligible"
    async with case["db"].immediate() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "chore-root")
                           .values(status="COMPLETED"))
    await case["db"].update_project("p", hierarchical_integration_mode="disabled")
    assert (await _grant(case, "chore-root", case["second"]))["outcome"] == "not_eligible"
    assert await _grants(case) == []


async def _train_collected(case) -> None:
    """``e1`` as the train leaves an epic it collected itself (``EpicCompletions``).

    The hierarchy checkpoint the grant is keyed on still awaits its children and
    was never verified, exactly as fleet-ridge-45's was when its PR sat CLEAN.
    """
    async with case["db"].immediate() as conn:
        await conn.execute(update(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == "e1").values(
            state="awaiting_children", checkpoint_sha=case["base"], verified_sha=None,
            verified_generation=None, current_verification_id=None))
        assert await case["producer"]._pull_request_source_on(conn, "e1") is None


def _train(*blockers):
    calls = []

    async def train_blockers(task_id):
        calls.append(task_id)
        return {"task_id": task_id, "blockers": list(blockers)}

    train_blockers.calls = calls
    return train_blockers


async def test_train_collected_epic_root_is_answered_with_what_the_train_waits_on(case):
    """fleet-ridge-45: a green, mergeable epic PR got a bare ``not_eligible``."""
    await _continuous_policy(case)
    await _train_collected(case)
    queued = {"code": "queued_behind_open_batch", "ref": "batch-dev",
              "detail": "refs/heads/main is held by open batch batch-dev (testing)"}
    train = _train(queued)
    service = RootAuthorization(case["db"], clock=lambda: 2000.0, train_blockers=train)

    preview = await service.run("e1")
    assert preview["outcome"] == "already_authorized"
    assert preview.get("authorized_by") is None
    assert (preview["task_type"], preview["pr_url"]) == (
        "feature", "https://github.com/o/r/pull/7")
    assert "admits this completed root without a per-source grant" in preview["reason"]
    assert ("queued_behind_open_batch: refs/heads/main is held by open batch batch-dev"
            in preview["reason"])
    applied = await service.run("e1", dry_run=False, expected_head_sha=case["first"],
                                reason="r", operator_id="supervisor session:s")
    assert applied["outcome"] == "already_authorized"
    assert train.calls == ["e1", "e1"]
    assert await _grants(case) == []

    quiet = await RootAuthorization(case["db"], train_blockers=_train()).run("e1")
    assert "no train blocker is recorded" in quiet["reason"]
    # Without the train only a verified exact source is admitted; still say why.
    bare = await _service(case).run("e1")
    assert bare["outcome"] == "not_eligible"
    assert "not verified at its head" in bare["reason"]


@pytest.mark.parametrize(("change", "reason"), [
    ("pr", "the root has no PR; `aq integration redrive-root` opens it"),
    ("origin", "the root has no live branch origin"),
    ("status", "the root is IN_PROGRESS, not COMPLETED"),
    ("mode", "project integration mode is 'disabled', not 'train'"),
    ("branch", "the root has no recorded branch"),
    ("child", "not a root: its parent e1 carries it"),
])
async def test_train_root_refusal_names_the_condition_that_failed(case, change, reason):
    await _continuous_policy(case)
    await _train_collected(case)
    values = {"pr": {"pr_url": None}, "status": {"status": "IN_PROGRESS"},
              "branch": {"branch_name": None}}.get(change)
    async with case["db"].immediate() as conn:
        if values:
            await conn.execute(update(tasks).where(tasks.c.id == "e1").values(**values))
        if change == "origin":
            await conn.execute(update(task_branch_origins).where(
                task_branch_origins.c.task_id == "e1").values(retired_at=1.0))
    if change == "mode":
        await case["db"].update_project("p", hierarchical_integration_mode="disabled")
    train = _train()
    result = await RootAuthorization(case["db"], train_blockers=train).run(
        "c1" if change == "child" else "e1")
    assert (result["outcome"], result["reason"]) == ("not_eligible", reason)
    assert train.calls == []


async def test_train_root_under_reviewed_admission_waits_for_an_approved_review(case):
    policy = await _continuous_policy(case)
    policy["root"]["admission"] = "reviewed"
    await case["db"].update_project("p", hierarchical_integration_policy=policy)
    await _train_collected(case)
    red = {"code": "pr_checks_red", "detail": "root e1 PR admission: pr_checks_red", "ref": "e1"}
    result = await RootAuthorization(case["db"], train_blockers=_train(red)).run("e1")
    assert result["outcome"] == "blocked"
    assert "after an approved review of its PR head" in result["reason"]
    assert "pr_checks_red: root e1 PR admission" in result["reason"]
