"""Cutover invariants for ``integration.git_first: active``.

Plan ``projects/agent-queue/plans/2026-10-04-git-first-integration-plan-2.md``
section 4 ("Single-engine transition") and section 2.2 ("No journal"). These
are the properties the operator's canary depends on, so they are asserted
against the durable tables rather than only against the wiring:

* a pending legacy outbox event is never dispatched into an old writer, before
  or after a restart;
* the train writes no green continuation, so nothing re-emits a promotion;
* the reduced modules read no checkpoint and write no ref journal;
* the stage-2 revisions add columns and constraints only: no column is dropped,
  no table is created, and none of them is a ref/OID journal.

Nothing here starts, stops or configures the daemon, and no test transfers a
root: ``_transfer`` stays an operator action.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from src.database.tables import integration_outbox, integration_subject_journal
from src.integration.outbox import enqueue_integration_event
from src.integration.service import IntegrationService
from tests.test_integration_train_sources import world  # noqa: F401 - shared real-Git fixture

#: The reduced protocol's own modules. Every guard these run reads git, checks
#: or review evidence; a checkpoint or journal reference here would be a
#: surviving legacy reader on the active path.
ACTIVE_MODULES = (
    "src/integration/git_truth.py",
    "src/integration/lock.py",
    "src/integration/checks.py",
    "src/integration/reviews.py",
    "src/integration/batches.py",
    "src/integration/epics.py",
    "src/integration/train.py",
    "src/integration/train_sources.py",
    "src/integration/source_trailer.py",
    "src/integration/shadow.py",
)

#: The stage-2 revisions, in chain order from the pre-cutover head.
STAGE_TWO_REVISIONS = (
    "a00000000073_integration_ref_leases",
    "a00000000074_git_batch_inputs",
    "a00000000075_integration_check_evidence_commit_cache",
)

ROOT = Path(__file__).resolve().parents[1]


def _upgrade_calls(revision: str) -> set[str]:
    """The alembic calls a revision's ``upgrade`` body actually makes."""
    tree = ast.parse((ROOT / "migrations/versions" / f"{revision}.py").read_text())
    upgrade = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "upgrade")
    return {node.func.attr for node in ast.walk(upgrade)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}


# -- Old-event isolation ------------------------------------------------------

@pytest.fixture
async def legacy_events(reuse_database):
    """One due legacy event, already pending before the selector flips to active."""
    db = await reuse_database("integration-cutover.db")
    async with db.immediate() as conn:
        await enqueue_integration_event(
            conn, event_id="legacy-1", dedup_key="legacy:1", project_id="p",
            event_type="integration.root_delivered",
            payload={"project_id": "p", "event_id": "legacy-1"}, available_at=0.0,
        )
    return db


def _active_service(now: float = 100.0) -> tuple[IntegrationService, list, list]:
    """The service as the daemon builds it under ``git_first: active``."""
    ticked, dispatched = [], []

    async def train_tick(at):
        ticked.append(at)
        return {"started": [], "running": [], "skipped": []}

    async def dispatch_due(at):
        dispatched.append(at)

    async def stop():
        return None

    service = IntegrationService(
        object(), SimpleNamespace(dispatch_due=dispatch_due),
        train=SimpleNamespace(tick=train_tick, stop=stop),
        clock=lambda: now,
    )
    return service, ticked, dispatched


async def test_pending_legacy_event_reaches_no_writer_before_or_after_a_restart(legacy_events):
    """The durable row stays pending across two services; nothing consumes it."""
    db = legacy_events
    for _restart in range(2):
        service, ticked, dispatched = _active_service()
        await service.tick(100.0)
        await service.stop()
        assert ticked == [100.0]
        assert dispatched == []
    async with db._engine.connect() as conn:
        pending = (await conn.execute(select(integration_outbox))).mappings().all()
    assert [row["id"] for row in pending] == ["legacy-1"]
    assert pending[0]["attempts"] == 0, "an active-mode pass must not consume the event"


async def test_green_continuation_is_never_emitted_by_the_active_path(legacy_events):
    """A pass adds no promotion continuation; only the legacy repair path can."""
    db = legacy_events
    service, _ticked, _dispatched = _active_service()
    await service.tick(100.0)
    await service.stop()
    async with db._engine.connect() as conn:
        emitted = (await conn.execute(
            select(integration_outbox.c.id, integration_outbox.c.event_type)
        )).all()
    assert emitted == [("legacy-1", "integration.root_delivered")]


async def test_the_reduced_path_writes_no_ref_journal(legacy_events):
    """The subject journal is a legacy record; an active pass leaves it untouched."""
    db = legacy_events
    service, _ticked, _dispatched = _active_service()
    await service.tick(100.0)
    await service.stop()
    async with db._engine.connect() as conn:
        entries = (await conn.execute(select(integration_subject_journal))).all()
    assert entries == []


# -- No surviving legacy reader ----------------------------------------------

@pytest.mark.parametrize("module", ACTIVE_MODULES)
def test_reduced_module_reads_no_checkpoint_and_writes_no_journal(module):
    """Static proof: the active path never names a checkpoint or journal table."""
    tree = ast.parse((ROOT / module).read_text())
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "src.database.tables"
        ):
            named.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
    assert not [name for name in named if "checkpoint" in name], module
    assert not [name for name in named if "journal" in name], module


def test_green_continuation_lives_only_in_the_legacy_repair_path():
    """The emitter exists, and only the retired repair path can call it."""
    callers = [str(path.relative_to(ROOT)) for path in sorted((ROOT / "src").rglob("*.py"))
               if path.name != "green_continuation.py"
               and "enqueue_green_continuation_on" in path.read_text()]
    assert callers == ["src/integration/repair.py"]


def test_the_selector_installs_no_diagnostics_in_active_mode():
    """``active`` builds no shadow comparison; shadow never becomes a policy input."""
    from src.integration.shadow import diagnostics_for

    active = SimpleNamespace(git_first="active")
    assert diagnostics_for(active, object(), object()) is None
    assert diagnostics_for(SimpleNamespace(git_first="shadow"), object(), object()) is not None


def test_shadow_never_acts_on_a_subject_the_reconciler_does_not_own():
    """Diagnostics run only for a reconciler-owned subject, and are read-only.

    The reconciler gates the optional comparison on ownership and records it
    only in the log and the correlation context, so shadow can never become a
    second writer or a policy input.
    """
    source = (ROOT / "src/integration/reconciler.py").read_text()
    assert "subject.engine is SubjectEngine.RECONCILER" in source
    assert "self._diagnostics(subject, facts)" in source
    assert "Diagnostic failure must not change the authoritative policy" in source


# -- Compatible schema --------------------------------------------------------

@pytest.mark.parametrize("revision", STAGE_TWO_REVISIONS)
def test_stage_two_revision_adds_columns_and_constraints_only(revision):
    """No stage-2 upgrade drops a column or creates a table."""
    calls = _upgrade_calls(revision)
    assert "drop_column" not in calls
    assert "drop_table" not in calls
    assert "create_table" not in calls
    assert calls & {"add_column", "create_index", "create_check_constraint"}


def test_stage_two_introduces_no_ref_journal_table():
    """No stage-2 revision names a table that replays refs, OIDs or batches."""
    forbidden = ("journal", "checkpoint", "intents", "outbox", "promotions", "deliveries")
    for revision in STAGE_TWO_REVISIONS:
        text = (ROOT / "migrations/versions" / f"{revision}.py").read_text()
        named = [line.split('"')[1] for line in text.splitlines()
                 if line.startswith("TABLE") and '"' in line]
        for name in named:
            assert not any(word in name for word in forbidden), (revision, name)


def test_the_check_cache_reshapes_in_place_and_adds_no_table():
    """``integration_check_evidence`` is reshaped, never replaced by a new table."""
    retained = json.loads(
        (ROOT / "tests/integration_ownership.json").read_text()
    )["baseline"]["tables"]
    assert "integration_commit_checks" not in retained
    assert "integration_check_evidence" in retained
    assert len(retained) == 45, "stage two adds no table; the predicate stays at 45"


# Promotion-flow cutover (G1 / B14). These tests never contact the live daemon.

FLOW = [{"id": "release", "source": "dev", "target": "main"}]


@pytest.fixture
async def cutover_world(reuse_database, request):
    from unittest.mock import AsyncMock

    from src.integration.cutover import Cutover
    from src.models import Project, RepoConfig, RepoSourceType, Task, TaskStatus

    db = await reuse_database("promotion-cutover")
    await db.create_project(Project(id="p", name="project",
        repo_url="https://github.com/acme/widgets.git", repo_default_branch="main"))
    await db.create_repo(RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.CLONE,
        url="https://github.com/acme/widgets.git", default_branch="main"))
    from sqlalchemy import update
    from src.database.tables import projects

    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            integration_repository_id="repo", hierarchical_integration_generation=7,
            hierarchical_integration_mode="train", hierarchical_integration_policy={
                "train": {"cadence_seconds": 42, "settling_cap_seconds": 84}}))
    for task_id, parent in (("done", None), ("epic", None), ("child", "epic")):
        await db.create_task(Task(id=task_id, project_id="p", title=task_id,
            description="fixture", status=TaskStatus.COMPLETED, parent_task_id=parent,
            branch_name="aq/epic/epic" if task_id == "epic" else "aq/" + task_id))
    from src.database.tables import task_branch_origins, task_delivery_receipts

    async with db.immediate() as conn:
        for task_id in ("done", "epic", "child"):
            await conn.execute(insert(task_branch_origins).values(
                id="origin-" + task_id, task_id=task_id, repository_id="repo",
                branch_name="aq/" + task_id, parent_ref="aq/epic/epic" if task_id == "child" else "main",
                parent_task_id="epic" if task_id == "child" else None,
                base_sha="a" * 40, creation_generation=0, created_at=0,
                reserved=True, materialized=True, materialized_at=1))
        for id_, repo, target in (("old", "repo", getattr(request, "param", "main")), ("existing-dev", "repo", "dev"),
                                  ("other-repo", "unrelated", "main")):
            await conn.execute(insert(task_delivery_receipts).values(
                id=id_, domain_key=id_, source_task_id="done" if id_ == "existing-dev" else "epic",
                repository_id=repo,
                target_branch=target, disposition="code", created_at=0))
    facts = {"cut_oid": "a" * 40, "target_oid": None,
             "workflow_branches": ["main", "dev", "staging"],
             "open_prs": [{"number": 12, "html_url": "https://github.com/acme/widgets/pull/12",
                           "title": "pending change"}]}
    facts["remote_state"] = {"default_branch": "main", "rulesets": [], "protection": {}}
    facts["manifest"] = {"promotion_attestation_names": [
        "Agent Queue Promotion Attestation (release)"]}
    port = SimpleNamespace(observe=AsyncMock(return_value=facts), prepare=AsyncMock(),
                           prepare_flow=AsyncMock(), verify=AsyncMock(),
                           retained=AsyncMock(return_value=["child", "done"]))
    return db, Cutover(db, port, clock=lambda: 100), port


async def apply_cutover(service, project_id, flow, **kwargs):
    """Apply a freshly captured operator preview unless a race test supplies one."""
    if "baseline" not in kwargs:
        kwargs["baseline"] = await service.plan(project_id, flow,
            reverse=kwargs.get("reverse", False), allow_epics=kwargs.get("allow_epics", False))
    return await service.run(project_id, flow, **kwargs)


async def cutover_rows(db):
    from src.database.tables import events, projects, repos, task_branch_origins, task_delivery_receipts

    async with db._engine.connect() as conn:
        return {table.name: [dict(row) for row in (await conn.execute(
                    select(table).order_by(*table.primary_key.columns))).mappings()]
                for table in (projects, repos, task_branch_origins, task_delivery_receipts, events)}


async def cutover_batch(db, id_, target, *, intent="open", lifecycle="building"):
    from src.database.tables import integration_batches

    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(
            id=id_, project_id="p", repository_id="repo", request_id=id_, trigger="schedule",
            target_ref="refs/heads/" + target, intent=intent,
            source_manifest_digest="sha256:" + "3" * 64, base_sha="a" * 40,
            lifecycle=lifecycle, integration_branch="aq/batches/" + id_,
            policy_snapshot={}, artifact_snapshot={}, cleanup_state="pending", created_at=0,
            updated_at=0))


async def test_promotion_cutover_plan_and_dry_run_are_read_only(cutover_world):
    db, service, port = cutover_world
    before = await cutover_rows(db)
    plan = await service.plan("p", FLOW)
    assert plan["ready"] and plan["generation"] == 7
    assert plan["undelivered_completions"] == ["child", "done"]
    assert plan["open_prs"][0]["number"] == 12
    assert plan["cut_oid"] == "a" * 40
    assert plan["receipt_count"] == 1
    assert plan["origin_count"] == 2
    assert "flow_target_release" in [step["id"] for step in plan["steps"]]
    assert plan["cadence"] == {"cadence_seconds": 42, "settling_cap_seconds": 84}
    assert [step["id"] for step in plan["steps"]].index("workflow") < [
        step["id"] for step in plan["steps"]].index("binding")
    assert (await service.run("p", FLOW, operator_id="operator"))["outcome"] == "preview"
    assert await cutover_rows(db) == before
    port.prepare.assert_not_awaited()


async def test_cutover_rejects_an_epic_origin_changed_since_preview(cutover_world):
    from sqlalchemy import update
    from src.database.tables import task_branch_origins

    db, service, port = cutover_world
    plan = await service.plan("p", FLOW)
    async with db.immediate() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.task_id == "epic").values(retired_at=20))
    before = await cutover_rows(db)
    with pytest.raises(ValueError, match="plan changed; preview again: origin_ids"):
        await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                          baseline=plan, operator_id="operator")
    assert await cutover_rows(db) == before
    port.prepare.assert_not_awaited()


@pytest.mark.parametrize("intent,lifecycle,blocked", [
    ("open", "building", True), ("paused", "testing", True),
    ("open", "promoted", False), ("aborted", "aborted", False),
])
async def test_promotion_cutover_barrier_reads_root_batches(cutover_world, intent, lifecycle, blocked):
    db, service, port = cutover_world
    await cutover_batch(db, "root", "main", intent=intent, lifecycle=lifecycle)
    plan = await service.plan("p", FLOW, allow_epics=True)
    assert bool(plan["blockers"]) is blocked
    result = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                               allow_epics=True, operator_id="operator")
    assert result["outcome"] == ("blocked" if blocked else "configured")
    if blocked:
        port.prepare.assert_not_awaited()
        assert plan["inventory"]["batches"][0]["id"] == "root"


async def test_promotion_cutover_allow_epics_preserves_live_collection(cutover_world):
    from src.database.tables import (
        integration_batches, integration_branch_owners, integration_subjects, playbook_artifacts,
    )
    from src.integration.records import PolicyActivation

    db, service, _port = cutover_world
    await cutover_batch(db, "epic-batch", "aq/epic/epic")
    async with db.immediate() as conn:
        await conn.execute(insert(playbook_artifacts).values(
            artifact_sha256="cutover-artifact", playbook_id="parent", source_digest="source",
            contract_fingerprint="contracts", compiler_build="fixture", path="unused", created_at=0))
        await conn.execute(insert(integration_subjects).values(
            id="epic-subject", project_id="p", repository_id="repo", kind="parent_episode",
            subject_key="epic", task_id="epic", phase="admitting", policy_playbook_id="parent",
            policy_artifact_sha256="cutover-artifact", target_ref="refs/heads/aq/epic/epic",
            due_set_at=0, next_due_at=1, max_wait_seconds=60, created_at=0, updated_at=0))
        await conn.execute(insert(integration_branch_owners).values(
            id="epic-owner", repository_id="repo", ref="aq/epic/epic", owner_id="collector",
            owner_role="collector", fence_token=1, handoff_state="attached", created_at=0,
            updated_at=0))
    before = await cutover_rows(db)
    assert (await service.plan("p", FLOW))["inventory"]["subjects"][0]["id"] == "epic-subject"
    assert (await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                              operator_id="operator"))["outcome"] == "blocked"
    assert await cutover_rows(db) == before
    assert await PolicyActivation(db).has_active_work("p")
    changed = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                                allow_epics=True, operator_id="operator")
    assert changed["outcome"] == "configured"
    async with db._engine.connect() as conn:
        assert await conn.scalar(select(integration_batches.c.intent).where(
            integration_batches.c.id == "epic-batch")) == "open"
        assert await conn.scalar(select(integration_branch_owners.c.handoff_state)) == "attached"


async def test_promotion_cutover_reverse_restores_exact_prior_binding_and_scoped_fixes(cutover_world):
    db, service, port = cutover_world
    before = await cutover_rows(db)

    async def before_publication(*_args, **_kwargs):
        assert await cutover_rows(db) == before  # all writes follow Git verification

    port.prepare.side_effect = before_publication
    changed = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7, operator_id="operator")
    assert changed["outcome"] == "configured" and changed["generation"] == 8
    rows = await cutover_rows(db)
    assert rows["projects"][0]["repo_default_branch"] == rows["repos"][0]["default_branch"] == "dev"
    assert rows["projects"][0]["promotion_flow"][0]["source"] == "dev"
    assert rows["task_branch_origins"] == before["task_branch_origins"]
    assert rows["projects"][0]["default_branch_cutover"] == {
        "repository_id": "repo", "old_default": "main", "new_default": "dev",
        "generation": 8, "cutover_at": 100}
    assert {row["id"]: row["target_branch"] for row in rows["task_delivery_receipts"]} == {
        "old": "dev", "existing-dev": "dev", "other-repo": "main"}
    port.prepare.side_effect = None
    reversed_ = await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                                  expected_generation=8, operator_id="operator")
    assert reversed_["outcome"] == "configured" and reversed_["generation"] == 9
    restored = await cutover_rows(db)
    assert restored["repos"] == before["repos"]
    assert restored["projects"][0]["repo_default_branch"] == "main"
    assert restored["projects"][0]["promotion_flow"] is None
    assert restored["projects"][0]["default_branch_cutover"] is None
    assert restored["task_delivery_receipts"] == before["task_delivery_receipts"]
    assert restored["task_branch_origins"] == before["task_branch_origins"]
    assert reversed_["plan"]["workflow_branches"] == changed["plan"]["workflow_branches"]


@pytest.mark.parametrize("cutover_world", ["main", "refs/heads/main"], indirect=True)
@pytest.mark.parametrize("retired_origin", ["done", "epic"])
async def test_reverse_restores_exact_receipts_and_config_after_origin_retirement(
    cutover_world, retired_origin,
):
    from sqlalchemy import update
    from src.database.tables import task_branch_origins

    db, service, _port = cutover_world
    before = await cutover_rows(db)
    changed = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                                 operator_id="operator")
    assert changed["outcome"] == "configured"
    async with db.immediate() as conn:
        await conn.execute(update(task_branch_origins).where(
            task_branch_origins.c.id == "origin-" + retired_origin).values(retired_at=101))
    retired_origins = (await cutover_rows(db))["task_branch_origins"]

    reversed_ = await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                                   expected_generation=8, operator_id="operator")
    assert reversed_["outcome"] == "configured" and reversed_["generation"] == 9
    restored = await cutover_rows(db)
    assert restored["projects"] == [
        {**before["projects"][0], "hierarchical_integration_generation": 9}]
    assert restored["repos"] == before["repos"]
    assert restored["task_delivery_receipts"] == before["task_delivery_receipts"]
    assert restored["task_branch_origins"] == retired_origins


async def test_reverse_refuses_an_audit_without_exact_receipt_targets(cutover_world):
    import json
    from src.database.tables import events

    db, service, port = cutover_world
    await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                        operator_id="operator")
    async with db._engine.connect() as conn:
        prior = json.loads(await conn.scalar(select(events.c.payload).where(
            events.c.event_type == "integration.cutover")))
    del prior["receipt_targets"]
    await db.log_event("integration.cutover", project_id="p", payload=json.dumps(prior))
    before = await cutover_rows(db)
    port.observe.reset_mock()
    with pytest.raises(ValueError, match="cutover_audit_incomplete:"):
        await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                            expected_generation=8, operator_id="operator")
    assert await cutover_rows(db) == before
    port.observe.assert_not_awaited()


async def test_cutover_receipt_table_lock_timeout_refuses_and_rolls_back(cutover_world):
    from sqlalchemy import text

    db, service, _port = cutover_world
    before = await cutover_rows(db)
    plan = await service.plan("p", FLOW)
    async with db._engine.begin() as holder:
        await holder.execute(text("LOCK TABLE task_delivery_receipts IN ACCESS SHARE MODE"))
        with pytest.raises(ValueError, match="cutover_lock_timeout:"):
            await service.run("p", FLOW, dry_run=False, expected_generation=7,
                              baseline=plan, operator_id="operator")
    assert await cutover_rows(db) == before
    async with db._engine.connect() as conn:
        assert await conn.scalar(text("SELECT tgenabled::text FROM pg_trigger WHERE "
            "tgrelid = 'task_delivery_receipts'::regclass AND "
            "tgname = 'trg_task_delivery_receipts_update'")) == "O"


@pytest.mark.parametrize("cutover_world", ["main", "refs/heads/main"], indirect=True)
async def test_cutover_and_reverse_preserve_train_archive_delivery_authorization(cutover_world):
    from src.database.tables import task_delivery_receipts
    from src.integration.removal_guard import (
        assert_integration_permits_removal,
        undelivered_removal_holders,
    )

    db, service, _port = cutover_world
    # Without the root receipt matching the current default, this collected
    # child makes the archive guard return collected_not_promoted.
    async with db.immediate() as conn:
        await conn.execute(insert(task_delivery_receipts).values(
            id="collected-child", domain_key="collected-child", source_task_id="child",
            target_task_id="epic", repository_id="repo", target_branch="aq/epic/epic",
            disposition="code", created_at=0))

    async def assert_archive_authorized(default):
        async with db._engine.connect() as conn:
            branch, holders = await undelivered_removal_holders(
                conn, root_id="epic", ids=["epic", "child"], project_id="p", mode="train")
            assert (branch, holders) == (default, [])
            await assert_integration_permits_removal(
                db, conn, root_id="epic", ids=["epic", "child"], project_id="p",
                mode="train", mutation="archive")

    await assert_archive_authorized("main")
    changed = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                                operator_id="operator")
    assert changed["outcome"] == "configured"
    await assert_archive_authorized("dev")
    reversed_ = await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                                  expected_generation=8, operator_id="operator")
    assert reversed_["outcome"] == "configured"
    await assert_archive_authorized("main")


@pytest.mark.parametrize("failure", ["push", "generation", "workflow"])
async def test_promotion_cutover_failed_preconditions_change_no_configuration(cutover_world, failure):
    from src.git.manager import GitError

    db, service, port = cutover_world
    before = await cutover_rows(db)
    expected = 6 if failure == "generation" else 7
    if failure == "push":
        port.prepare.side_effect = GitError("push denied")
        with pytest.raises(GitError, match="push denied"):
            await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=expected,
                              operator_id="operator")
    else:
        if failure == "workflow":
            port.observe.return_value = {**port.observe.return_value, "workflow_branches": ["main"]}
        result = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=expected,
                                   operator_id="operator")
        assert result["outcome"] == ("stale" if failure == "generation" else "blocked")
        port.prepare.assert_not_awaited()
    assert await cutover_rows(db) == before


async def test_promotion_cutover_rechecks_a_batch_arriving_after_inventory(cutover_world):
    db, service, port = cutover_world
    before = await cutover_rows(db)
    facts = port.observe.return_value

    async def observe(*_args, **_kwargs):
        await cutover_batch(db, "raced-root", "main")
        return facts

    baseline = await service.plan("p", FLOW)
    port.observe.side_effect = observe
    result = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                                 baseline=baseline, operator_id="operator")
    assert result["outcome"] == "blocked"
    assert result["blockers"][0]["id"] == "raced-root"
    assert await cutover_rows(db) == before
    port.prepare.assert_not_awaited()


async def test_cutover_first_dev_batch_selects_retained_and_aborted_main_inputs(world):  # noqa: F811
    from unittest.mock import AsyncMock

    from sqlalchemy import update
    from src.database.tables import projects
    from src.git.github_contracts import GitHubRepositoryBinding
    from src.integration.batches import Batch, BatchMember, BatchStore
    from src.integration.cutover import Cutover, CutoverGit
    from src.integration.train import TrainTarget
    from tests.test_delivery_consumers import git
    from tests.test_integration_train_sources import completed, fixture_batches, snapshot, tree

    db, origin = world.db, world.origin
    workflow = origin.clone / ".github" / "workflows" / "tests.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on:\n  pull_request:\n    branches: [main, dev]\n")
    (origin.clone / ".github" / "agent-queue-integration.json").write_text(json.dumps({
        "promotion_attestation_names": ["Agent Queue Promotion Attestation (release)"]}))
    git(origin.clone, "add", ".")
    git(origin.clone, "commit", "-qm", "reviewed cutover prerequisites")
    git(origin.clone, "push", "-q", "origin", "main")
    aborted_source = await completed(world, "aborted", parent_ref="main")
    ordinary_source = await completed(world, "ordinary", parent_ref="main")
    await completed(world, "no-provenance", done=False, parent_ref="main")
    await db.update_task("no-provenance", status="COMPLETED")
    store = BatchStore(db)
    await store.freeze(Batch("old-main", "p", "r", "refs/heads/main"), (
        BatchMember("aborted", aborted_source, git(origin.clone, "rev-parse", aborted_source + "^")),
    ), trees={"aborted": tree(world, aborted_source)})
    await store.set_intent("old-main", "aborted", operator_id="operator", reason="retry")
    batches = fixture_batches(db)
    members, _, _ = await batches.pending(TrainTarget("p", "r", "refs/heads/main"),
                                         await snapshot(world))
    assert [member.task_id for member in members] == ["ordinary"]
    # The production fence permits local repositories only in development mode.
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_mode="development", repo_url=origin.url))
    client = SimpleNamespace(repository=GitHubRepositoryBinding(123, "acme/widgets"),
        request=AsyncMock(return_value=SimpleNamespace(body="[]")))
    port = CutoverGit(db, client)
    port.remote_state = AsyncMock(return_value={
        "default_branch": "main", "rulesets": [], "protection": {"main": None, "dev": None}})
    port.verify = AsyncMock()  # GitHub ruleset verification has separate API-fixture coverage.
    service = Cutover(db, port)
    plan = await service.plan("p", FLOW)
    assert plan["undelivered_completions"] == ["aborted", "ordinary"]
    assert len(plan["aborted_members"]) == 1
    assert {key: plan["aborted_members"][0][key] for key in (
        "batch_id", "task_id", "source_sha")} == {"batch_id": "old-main", "task_id": "aborted",
                                                  "source_sha": aborted_source}
    assert "abort_reason" in plan["aborted_members"][0]
    assert plan["aborted_members"][0]["aborted_at"] > 0
    applied = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=0,
                                operator_id="operator", baseline=plan)
    assert applied["outcome"] == "configured", applied
    assert git(origin.clone, "ls-remote", "origin", "refs/heads/dev").split()[0] == plan["cut_oid"]
    assert (await store.get("old-main")).intent == "aborted"
    dev = TrainTarget("p", "r", "refs/heads/dev")

    async def freeze(batch, members, **_kwargs):
        from dataclasses import replace

        members = tuple(replace(member, order=index) for index, member in enumerate(members))
        await store.freeze(batch, members, trees={m.task_id: tree(world, m.source_sha) for m in members})
        return batch

    first = await batches.open_batch(dev, await snapshot(world, dev),
        SimpleNamespace(store=store, freeze=freeze))
    assert {m.task_id: m.source_sha for m in first.members} == {
        "aborted": aborted_source, "ordinary": ordinary_source}
    assert await batches.eligible(first.batch, first.members)


@pytest.mark.parametrize("mismatch", ["workflow", "default", "rulesets"])
async def test_reverse_refuses_until_manual_github_state_is_restored(cutover_world, mismatch):
    from unittest.mock import AsyncMock

    from src.integration.cutover import CutoverGit

    db, service, port = cutover_world
    assert (await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                              operator_id="operator"))["outcome"] == "configured"
    before = await cutover_rows(db)
    facts = dict(port.observe.return_value)
    if mismatch == "workflow":
        facts["workflow_branches"] = ["main", "dev"]
        port.observe.return_value = facts
        result = await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                                   expected_generation=8, operator_id="operator")
        assert result["outcome"] == "blocked"
    else:
        real = CutoverGit(db, None)
        state = dict(facts["remote_state"])
        state["default_branch" if mismatch == "default" else "rulesets"] = (
            "dev" if mismatch == "default" else [{"id": 999}])
        real.remote_state = AsyncMock(return_value=state)
        port.verify.side_effect = real.verify
        with pytest.raises(ValueError, match="github_state_not_restored"):
            await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                              expected_generation=8, operator_id="operator")
    assert await cutover_rows(db) == before


async def test_cutover_refuses_a_changed_aborted_member_preview(cutover_world):
    from sqlalchemy import update
    from src.database.tables import integration_batches
    from src.integration.batches import Batch, BatchMember, BatchStore

    db, service, port = cutover_world
    store = BatchStore(db)
    await store.freeze(Batch("aborted", "p", "repo", "refs/heads/main"),
        (BatchMember("done", "b" * 40, "a" * 40),), trees={"done": "c" * 40})
    await store.set_intent("aborted", "aborted", operator_id="operator", reason="retry")
    plan = await service.plan("p", FLOW)
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "aborted").values(human_abort_reason="content rejected"))
    before = await cutover_rows(db)
    with pytest.raises(ValueError, match="plan changed; preview again: aborted_members"):
        await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                          baseline=plan, operator_id="operator")
    assert await cutover_rows(db) == before
    port.prepare.assert_not_awaited()


@pytest.mark.parametrize("failure_point", ["after_fixes", "guard_suspended", "cancelled"])
async def test_cutover_preserves_receipt_update_guard_and_rolls_back_mid_fix(
    cutover_world, monkeypatch, failure_point,
):
    import asyncio

    from sqlalchemy import event, text, update
    from sqlalchemy.exc import DBAPIError
    from src.database.tables import task_delivery_receipts
    from src.integration.cutover import _Activation

    db, service, _port = cutover_world
    before = await cutover_rows(db)
    original = _Activation.write_on

    async def fail_after_fixes(self, conn, project):
        await original(self, conn, project)
        raise ValueError("injected post-fix failure")

    def fail_during_receipt_update(conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE task_delivery_receipts SET target_branch"):
            assert conn.scalar(text("SELECT current_setting('lock_timeout')")) == "5s"
            assert conn.scalar(text("SELECT tgenabled::text FROM pg_trigger WHERE "
                "tgrelid = 'task_delivery_receipts'::regclass AND "
                "tgname = 'trg_task_delivery_receipts_update'")) == "D"
            if failure_point == "cancelled":
                asyncio.current_task().cancel("injected post-fix failure")
                return
            raise ValueError("injected post-fix failure")

    if failure_point in {"guard_suspended", "cancelled"}:
        event.listen(db._engine.sync_engine, "before_cursor_execute", fail_during_receipt_update)
    try:
        with monkeypatch.context() as context:
            if failure_point == "after_fixes":
                context.setattr(_Activation, "write_on", fail_after_fixes)
            with pytest.raises(asyncio.CancelledError if failure_point == "cancelled" else ValueError,
                               match="post-fix failure"):
                await asyncio.create_task(apply_cutover(
                    service, "p", FLOW, dry_run=False, expected_generation=7,
                    operator_id="operator"))
    finally:
        if failure_point in {"guard_suspended", "cancelled"}:
            event.remove(db._engine.sync_engine, "before_cursor_execute", fail_during_receipt_update)
    assert await cutover_rows(db) == before
    with pytest.raises(DBAPIError, match="append-only"):
        async with db.immediate() as conn:
            await conn.execute(update(task_delivery_receipts).where(
                task_delivery_receipts.c.id == "old").values(target_branch="unauthorized"))
    assert (await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                              operator_id="operator"))["outcome"] == "configured"
    for reverse, generation in ((False, 8), (True, 9)):
        if reverse:
            assert (await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                expected_generation=generation - 1, operator_id="operator"))["outcome"] == "configured"
        with pytest.raises(DBAPIError, match="append-only"):
            async with db.immediate() as conn:
                await conn.execute(update(task_delivery_receipts).where(
                    task_delivery_receipts.c.id == "old").values(target_branch="unauthorized"))


@pytest.mark.parametrize("failure", [
    None, "default", "bypass", "attestation", "tags", "trust", "hidden_actors", "null_actors",
])
async def test_cutover_verifies_operator_branch_and_tag_rulesets(cutover_world, failure):
    from unittest.mock import AsyncMock
    from src.git.github_contracts import (
        GitHubCredentialIdentity, GitHubCredentialMode, GitHubRepositoryBinding,
    )
    from src.integration.cutover import CutoverGit
    from src.integration.trust_manifest import ATTESTATION_NAME, build_trust_manifest

    db, service, port = cutover_world
    name = "Agent Queue Promotion Attestation (release)"
    manifest = build_trust_manifest(canonical_repository_id="repo", repository_id=123,
        full_name="acme/widgets", ci_producer_app_id=11, attestation_app_id=22,
        checks=["tests"], check_version="v1", promotion_attestation_names=[name])
    port.observe.return_value["manifest"] = manifest
    flow = [{**FLOW[0], "versioning": {"kind": "semver_tag", "source": "pyproject"}}]
    plan = await service.plan("p", flow)
    state = {"default_branch": "dev", "protection": {"dev": None, "main": None}, "rulesets": [
        {"id": 3, "target": "tag", "enforcement": "active",
         "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
         "bypass_actors": [{"actor_id": 22, "actor_type": "Integration", "bypass_mode": "always"}],
         "rules": [{"type": "creation"}]},
        {"id": 4, "target": "tag", "enforcement": "active",
         "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
         "bypass_actors": [], "rules": [{"type": "update"}, {"type": "deletion"}]},
    ]}
    if failure == "default":
        state["default_branch"] = "main"
    if failure == "tags":
        state["rulesets"][1]["bypass_actors"] = [{"actor_id": 22}]
    if failure == "hidden_actors":
        state["rulesets"][1].pop("bypass_actors")
    if failure == "null_actors":
        state["rulesets"][1]["bypass_actors"] = None

    async def effective(path, **_kwargs):
        root = "/dev?" in path
        return [{"ruleset_id": 1 if root else 2, "type": "required_status_checks",
                 "parameters": {"required_status_checks": [{
                     "context": ATTESTATION_NAME if root or failure == "attestation" else name,
                     "integration_id": 22}]}}]

    client = SimpleNamespace(repository=GitHubRepositoryBinding(123, "acme/widgets"),
        credential_identity=GitHubCredentialIdentity(GitHubCredentialMode.APP,
            app_id=99 if failure == "trust" else 22, installation_id=33),
        paged_list=AsyncMock(side_effect=effective), request_json=AsyncMock(return_value={
            "current_user_can_bypass": "always" if failure == "bypass" else "never"}))
    real = CutoverGit(db, client)
    real.remote_state = AsyncMock(return_value=state)
    if failure:
        with pytest.raises(ValueError, match="github_"):
            await real.verify(plan, policy=None)
    else:
        await real.verify(plan, policy=None)


async def test_cutover_remote_snapshot_records_operator_visible_bypass_actors():
    from unittest.mock import AsyncMock
    from src.git.github_contracts import (
        GitHubAccessError, GitHubCredentialIdentity, GitHubRepositoryBinding,
    )
    from src.integration.cutover import CutoverGit
    from src.integration.protection import LocalRulesetReader

    binding = GitHubRepositoryBinding(123, "acme/widgets")
    hidden = {"id": 3, "name": "tag creation", "target": "tag", "enforcement": "active",
              "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
              "rules": [{"type": "creation"}], "current_user_can_bypass": "always"}
    actors = [{"actor_id": 22, "actor_type": "Integration", "bypass_mode": "always"}]

    async def app_read(method, path):
        assert method == "GET"
        if path == "/repositories/123":
            return {"default_branch": "main"}
        if path == "/repositories/123/rulesets/3":
            return hidden
        raise GitHubAccessError("not_found_or_hidden", "no classic protection")

    app = SimpleNamespace(repository=binding, request_json=AsyncMock(side_effect=app_read),
                          paged_list=AsyncMock(return_value=[{"id": 3}]))
    operator = SimpleNamespace(repository=binding,
        credential_identity=GitHubCredentialIdentity.existing_login(),
        request_json=AsyncMock(return_value={**hidden, "bypass_actors": actors,
                                            "current_user_can_bypass": "never"}))
    port = CutoverGit(None, app, ruleset_admin_reader=LocalRulesetReader(operator))
    state = await port.remote_state({"main"})
    assert state["rulesets"][0]["bypass_actors"] == actors
    assert "current_user_can_bypass" not in state["rulesets"][0]
    assert state["protection"] == {"main": None}
    assert "bypass_actors" not in hidden
    operator.request_json.assert_awaited_once_with("GET", "/repositories/123/rulesets/3")
    with pytest.raises(ValueError, match="github_ruleset_unverifiable"):
        await CutoverGit(None, app).remote_state({"main"})


@pytest.mark.parametrize("actors", ["missing", None])
async def test_cutover_refuses_saved_baseline_with_hidden_actors(cutover_world, actors):
    db, service, port = cutover_world
    baseline = await service.plan("p", FLOW)
    row = {"id": 3, "target": "tag"}
    if actors != "missing":
        row["bypass_actors"] = actors
    baseline["remote_state"]["rulesets"] = [row]
    with pytest.raises(ValueError, match="saved_plan_invalid"):
        await service.run("p", FLOW, expected_generation=baseline["generation"],
                          dry_run=False, operator_id="operator", baseline=baseline)


@pytest.mark.parametrize("read_only", [False, True])
async def test_worker_cannot_run_cutover_or_read_its_operator_plan(read_only):
    from unittest.mock import AsyncMock
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import (
        ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL, principal_context,
    )

    db = SimpleNamespace(get_project=AsyncMock())
    handler = SimpleNamespace(db=db)
    with principal_context(ExecutionPrincipal(PrincipalKind.SESSION, TRUSTED_LOCAL.policy,
                                              project_id="p", session_id="worker")):
        result = await IntegrationCommandsMixin._integration_cutover(
            handler, {"project_id": "p", "flow": FLOW}, read_only=read_only)
    assert result["success"] is False and result["outcome"] == "unauthorized"
    db.get_project.assert_not_awaited()


@pytest.mark.parametrize("apply", [False, True])
async def test_supervisor_cannot_apply_or_preview_receipt_cutover(apply):
    from unittest.mock import AsyncMock
    from src.commands.integration_commands import IntegrationCommandsMixin
    from src.commands.principal import (
        ExecutionPrincipal, PrincipalKind, TRUSTED_LOCAL, principal_context,
    )

    db = SimpleNamespace(get_project=AsyncMock(), get_session=AsyncMock(return_value=SimpleNamespace(
        id="supervisor", profile_id="supervisor", lifecycle="named", state="running",
        desired_state="running", project_id="p")))
    with principal_context(ExecutionPrincipal(PrincipalKind.SESSION, TRUSTED_LOCAL.policy,
        project_id="p", session_id="supervisor", elevated=True)):
        result = await IntegrationCommandsMixin._integration_cutover(
            SimpleNamespace(db=db), {"project_id": "p", "flow": FLOW,
                "dry_run": not apply, "baseline": {} if apply else None}, read_only=False)
    assert result["outcome"] == "unauthorized" and "local operator" in result["error"]
    db.get_project.assert_not_awaited()


@pytest.mark.parametrize("failure", ["denied", "wrong_oid", "source_moved"])
async def test_cutover_exact_oid_publication_failure_preserves_all_rows(cutover_world, failure):
    from unittest.mock import AsyncMock
    from src.git.manager import GitError, RemoteRefResult, RemoteRefState
    from src.integration.cutover import CutoverGit

    db, service, port = cutover_world
    git = SimpleNamespace(als_remote_ref=AsyncMock(side_effect=[
        RemoteRefResult(RemoteRefState.ABSENT),
        RemoteRefResult(RemoteRefState.PRESENT, oid="b" * 40)]),
        _apush_oid=AsyncMock(side_effect=GitError("denied") if failure == "denied" else None))
    real = CutoverGit(db, None)
    real.target_oid = None
    real.repository = await db.get_repo("repo")
    real.snapshot = SimpleNamespace(target_oid="a" * 40,
        is_fresh=AsyncMock(return_value=failure != "source_moved"),
        observation=SimpleNamespace(git=git, store="unused"))
    port.prepare.side_effect = real.prepare
    before = await cutover_rows(db)
    with pytest.raises((ValueError, GitError)):
        await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7, operator_id="operator")
    assert await cutover_rows(db) == before
    if failure != "source_moved":
        git._apush_oid.assert_awaited_once_with("unused", "a" * 40, "dev",
            expected_old_oid="0" * 40,
            repository_url=real.repository.url)


async def test_cutover_apply_requires_a_saved_plan_inside_service(cutover_world):
    db, service, port = cutover_world
    before = await cutover_rows(db)
    with pytest.raises(ValueError, match="requires the saved cutover plan"):
        await service.run("p", FLOW, dry_run=False, expected_generation=7, operator_id="operator")
    assert await cutover_rows(db) == before
    port.prepare.assert_not_awaited()


@pytest.mark.parametrize("phase", ["before_apply", "before_write"])
async def test_cutover_refuses_receipt_set_changes_after_preview(cutover_world, phase):
    from src.database.tables import task_delivery_receipts

    db, service, port = cutover_world
    plan = await service.plan("p", FLOW)

    async def new_receipt(*_args, **_kwargs):
        async with db.immediate() as conn:
            await conn.execute(insert(task_delivery_receipts).values(
                id="raced", domain_key="raced", source_task_id="done", repository_id="repo",
                target_branch="refs/heads/main", disposition="code", created_at=2))

    if phase == "before_apply":
        await new_receipt()
    else:
        port.prepare.side_effect = new_receipt
    with pytest.raises(ValueError, match="plan changed; preview again: receipt"):
        await service.run("p", FLOW, dry_run=False, expected_generation=7,
                          baseline=plan, operator_id="operator")
    rows = await cutover_rows(db)
    assert rows["projects"][0]["repo_default_branch"] == "main"
    assert rows["projects"][0]["default_branch_cutover"] is None
    assert {row["id"]: row["target_branch"] for row in rows["task_delivery_receipts"]}["old"] == "main"


async def test_cutover_rebinds_all_old_roots_and_preserves_later_hotfix_origins(cutover_world):
    from sqlalchemy import text, update
    from sqlalchemy.exc import DBAPIError
    from src.database.tables import archived_tasks, task_branch_origins
    from src.integration.delivery_observer import delivery_targets
    from src.integration.train_sources import _pending_tasks
    from src.models import Task, TaskStatus

    db, service, _port = cutover_world

    async def root(task_id, created_at, status=TaskStatus.COMPLETED, *, archived=False):
        if archived:
            async with db.immediate() as conn:
                await conn.execute(insert(archived_tasks).values(
                    id=task_id, project_id="p", title=task_id, description="fixture",
                    status="COMPLETED", created_at=created_at, updated_at=created_at, archived_at=5))
        else:
            await db.create_task(Task(id=task_id, project_id="p", title=task_id,
                description="fixture", status=status))
        async with db.immediate() as conn:
            await conn.execute(insert(task_branch_origins).values(
                id="origin-" + task_id, task_id=task_id, repository_id="repo",
                branch_name="aq/" + task_id, parent_ref="refs/heads/main", base_sha="a" * 40,
                reserved=True, materialized=True, created_at=created_at, creation_generation=0))

    await root("open-root", 2, TaskStatus.IN_PROGRESS)
    await root("archived-root", 3, archived=True)
    before = await cutover_rows(db)
    result = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                                 operator_id="operator")
    assert result["plan"]["origin_ids"] == [
        "origin-archived-root", "origin-done", "origin-epic", "origin-open-root"]
    assert (await cutover_rows(db))["task_branch_origins"] == before["task_branch_origins"]
    await root("hotfix", 101)
    await root("boundary-hotfix", 100)
    ids = ["done", "epic", "child", "open-root", "hotfix", "boundary-hotfix", "archived-root"]
    async with db._engine.connect() as conn:
        targets = await delivery_targets(conn, ids, reduced=True)
        assert {id_: target.target_ref for id_, target in targets.items()} == {
            "done": "refs/heads/dev", "epic": "refs/heads/dev", "open-root": "refs/heads/dev",
            "child": "refs/heads/aq/epic/epic", "hotfix": "refs/heads/main",
            "boundary-hotfix": "refs/heads/main", "archived-root": "refs/heads/dev"}
        assert set(await _pending_tasks(conn, "p", "repo", limit=None)) == {"done", "epic", "child"}
        assert await conn.scalar(text("SELECT tgenabled::text FROM pg_trigger WHERE "
            "tgrelid = 'task_branch_origins'::regclass AND "
            "tgname = 'trg_task_branch_origins_materialized_update'")) == "O"
    with pytest.raises(DBAPIError, match="immutable"):
        async with db.immediate() as conn:
            await conn.execute(update(task_branch_origins).where(
                task_branch_origins.c.id == "origin-done").values(parent_ref="dev"))
    result = await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                                 expected_generation=8, operator_id="operator")
    assert result["outcome"] == "configured"
    async with db._engine.connect() as conn:
        targets = await delivery_targets(conn, ids, reduced=True)
        assert targets["done"].target_ref == targets["hotfix"].target_ref == "refs/heads/main"


async def test_cutover_receipts_restore_each_original_branch_spelling(cutover_world):
    from src.database.tables import task_delivery_receipts

    db, service, _port = cutover_world
    async with db.immediate() as conn:
        await conn.execute(insert(task_delivery_receipts).values(
            id="qualified", domain_key="qualified", source_task_id="epic", repository_id="repo",
            target_branch="refs/heads/main", disposition="code", created_at=0))
    before = await cutover_rows(db)
    result = await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                                 operator_id="operator")
    assert result["plan"]["receipt_targets"] == {"old": "main", "qualified": "refs/heads/main"}
    async with db._engine.connect() as conn:
        assert dict((await conn.execute(select(task_delivery_receipts.c.id,
            task_delivery_receipts.c.target_branch))).all()) == {
                "old": "dev", "qualified": "refs/heads/dev", "existing-dev": "dev",
                "other-repo": "main"}
    await apply_cutover(service, "p", None, reverse=True, dry_run=False,
                        expected_generation=8, operator_id="operator")
    assert (await cutover_rows(db))["task_delivery_receipts"] == before["task_delivery_receipts"]


async def test_cutover_abort_time_survives_later_batch_cleanup(cutover_world):
    from sqlalchemy import update
    from src.database.tables import integration_batches
    from src.integration.batches import Batch, BatchMember, BatchStore

    db, service, _port = cutover_world
    await db.log_event("unrelated.event", project_id="p", payload="legacy plain text event")
    store = BatchStore(db)
    await store.freeze(Batch("aborted", "p", "repo", "refs/heads/main"),
        (BatchMember("done", "b" * 40, "a" * 40),), trees={"done": "c" * 40})
    await store.set_intent("aborted", "aborted", operator_id="operator", reason="retry")
    before = (await service.plan("p", FLOW))["aborted_members"][0]["aborted_at"]
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "aborted").values(updated_at=before + 1000,
                                                         cleanup_state="complete"))
    assert (await service.plan("p", FLOW))["aborted_members"][0]["aborted_at"] == before


async def test_completion_provenance_uses_resolved_root_after_cutover(cutover_world, monkeypatch):
    from unittest.mock import AsyncMock
    from src.integration.provenance import record_worker_completion

    db, service, _port = cutover_world
    await apply_cutover(service, "p", FLOW, dry_run=False, expected_generation=7,
                        operator_id="operator")
    source = "b" * 40
    store = SimpleNamespace(run=AsyncMock(return_value="a" * 40), exact=AsyncMock(),
                            write_completion=AsyncMock())
    monkeypatch.setattr("src.integration.hierarchy.resolve_workspace_checkpoint",
                        AsyncMock(return_value=source))
    monkeypatch.setattr("src.integration.provenance.GitProvenance", lambda *_a, **_kw: store)
    assert await record_worker_completion(db, None, await db.get_task("done"),
        await db.get_project("p"), "unused", "completion") == source
    store.run.assert_awaited_once_with("rev-parse", "--verify", "refs/remotes/origin/dev")
    store.write_completion.assert_awaited_once()


async def test_cutover_metadata_migration_is_idempotent_on_disposable_database(
    cutover_world, monkeypatch,
):
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    from sqlalchemy.dialects.postgresql import JSONB

    db, _service, _port = cutover_world
    revision = import_module("migrations.versions.a00000000086_default_branch_cutover")

    def exercise(conn):
        monkeypatch.setattr(revision, "op", Operations(MigrationContext.configure(conn)))
        revision.downgrade()
        revision.downgrade()
        assert "default_branch_cutover" not in {
            column["name"] for column in inspect(conn).get_columns("projects")}
        revision.upgrade()
        revision.upgrade()
        column = next(column for column in inspect(conn).get_columns("projects")
                      if column["name"] == "default_branch_cutover")
        assert isinstance(column["type"], JSONB) and column["nullable"]

    async with db.immediate() as conn:
        await conn.run_sync(exercise)
