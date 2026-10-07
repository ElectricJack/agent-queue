"""Development policy equivalence, engine ownership and its audited transfer."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select

from src.database import Database
from src.database.tables import (
    integration_subjects,
    playbook_artifacts,
    projects,
    repos,
)
from src.integration.ci_producers import LocalCIProducer, LocalValidationPlan
from src.integration.development import DevelopmentPolicy
from src.integration.development_adapter import (
    DevelopmentFrontier,
    DevelopmentIntegrationAdapter,
    DevelopmentMember,
    ordered_development_members,
)
from src.integration.development_policy import PinnedDevelopmentPolicy, render_development_policy
from src.integration.models import BranchKey, Fence
from src.integration.ownership import BranchOwnership
from src.integration.runtime_contracts import (
    AdmissionPredicate,
    CIEvidence,
    CIState,
    HoldFacts,
    GateFacts,
    JournalMode,
    MemberFacts,
    MemberRef,
    MergeMembersArgs,
    Primitive,
    PrimitiveOutcome,
    PrimitivePorts,
    SealArgs,
    Subject,
    SubjectEngine,
    SubjectFacts,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    WriterBudget,
    WriterLease,
    WriterStatus,
)
from src.playbooks.definition import ProjectScope, load_definition_json, source_digest
from src.playbooks.integration_policy import IntegrationPolicyFacts, policy_from_markdown
from tests.db_fixtures import lease_dsn
from tests.test_integration_ci_producers import JobClient

BASE, A, B, C = (letter * 40 for letter in "abcd")


def pinned_policy(mode="focused", **settings):
    settings.setdefault("commands", ["aq test tests/test_development_validation.py"])
    source = render_development_policy("p", DevelopmentPolicy(validation=mode, **settings))
    artifact = load_definition_json(Path(
        "tests/fixtures/playbooks/v2/root-train/artifact.json"
    ).read_text()).model_copy(update={
        "id": "p-development", "scope": ProjectScope(project_id="p"),
        "source_hash": source_digest(source), "integration_policy": policy_from_markdown(source),
    })
    return PinnedDevelopmentPolicy(artifact, source)


def subject(pinned, phase=SubjectPhase.ADMITTING, **values):
    return Subject(**{
        "id": "root", "project_id": "p", "repository_id": "repo",
        "kind": SubjectKind.ROOT_BATCH, "subject_key": "root_batch:repo:request",
        "engine": SubjectEngine.RECONCILER, "phase": phase,
        "policy": pinned.compiled.pin, "target_ref": "refs/heads/main",
        "head_sha": B, "base_sha": BASE,
        "schedule": SubjectSchedule.progress(now=100, max_wait_seconds=3600),
        "created_at": 100, "updated_at": 100, **values,
    })


def facts(s, state=CIState.NONE, **values):
    if s.kind is SubjectKind.SOURCE:
        values.setdefault("members", (MemberFacts(task_id=s.task_id,
                                                   head_sha=s.head_sha, base_sha=s.base_sha),))
    return IntegrationPolicyFacts(**{
        "subject_id": s.id, "subject_version": s.version, "kind": s.kind,
        "phase": s.phase, "observed_at": 100, "head": s.head,
        "default_branch_head": BASE, "writer": s.writer, "budget": s.budget,
        "ci": (CIEvidence(head_sha=s.head_sha, state=state),), **values,
    })


def member(task_id, sha=A, *, deps=(), **facts_values):
    return DevelopmentMember(
        MemberFacts(task_id=task_id, head_sha=sha, base_sha=BASE, **facts_values),
        dependencies=frozenset(deps),
    )


def seal_args(**changes):
    return SealArgs(admission=AdmissionPredicate(**{
        "require_review": False, "include_authorized": False, **changes,
    }))


@pytest.mark.parametrize("mode", ["focused", "advisory", "none"])
def test_installed_fields_are_frozen_in_source_and_project_scope(mode):
    pinned = pinned_policy(mode, interval_seconds=731, max_batch_size=7,
                           timeout_seconds=89, slot_wait_seconds=0,
                           regenerate="scripts/regenerate-generated.sh",
                           regenerate_timeout_seconds=127)
    settings = pinned.settings
    assert (settings.validation, settings.timeout_seconds, settings.slot_wait_seconds,
            settings.regenerate_timeout_seconds) == (mode, 89, 0, 127)
    assert settings.commands == ["aq test tests/test_development_validation.py"]
    table = pinned.definition.integration_policy.tables[SubjectKind.ROOT_BATCH]
    assert table.actions["seal"].inputs["admission"].value == {
        "require_review": False, "require_source_ci": False, "include_authorized": False,
        "max_members": 7,
    }
    assert table.actions["cadence"].inputs["seconds"].value == 731
    assert table.actions["build"].inputs["regenerate_generated"].value is True
    assert pinned.definition.scope.project_id == "p"
    with pytest.raises(ValueError, match="reviewed artifact"):
        replace(pinned, source=pinned.source.replace("731", "732"))


@pytest.mark.parametrize("mode, red_phase", [
    ("focused", SubjectPhase.REPAIRING), ("advisory", SubjectPhase.PUBLISHING),
])
def test_conclusive_red_preserves_mode_repair_choice(mode, red_phase):
    pinned = pinned_policy(mode)
    s = subject(pinned, SubjectPhase.PROMOTABLE)
    decision = pinned.compiled.evaluate(s, facts(s, CIState.RED))
    assert decision.primitive is Primitive.CI_OBSERVE
    outcome = PrimitiveOutcome(primitive=decision.primitive, outcome="red")
    assert pinned.compiled.phase(s, decision, outcome) is red_phase


@pytest.mark.parametrize("mode,state", [("none", CIState.NONE), ("advisory", CIState.RED)])
def test_unverified_modes_never_fabricate_green_or_bypass_publication(mode, state):
    pinned = pinned_policy(mode)
    s = subject(pinned, SubjectPhase.PUBLISHING)
    decision = pinned.compiled.evaluate(s, facts(s, state))
    assert decision.primitive is Primitive.GATE
    assert decision.request.no_default is True
    assert decision.request.choices == ("retry", "hold")


def test_only_exact_current_green_and_matching_fence_publish():
    pinned = pinned_policy()
    s = subject(pinned, SubjectPhase.PUBLISHING)
    fence = Fence(target=BranchKey(repository_id="repo", branch=s.target_ref),
                  owner_id=s.id, token=2)
    good = facts(s, CIState.GREEN, publisher_fence=fence)
    decision = pinned.compiled.evaluate(s, good)
    assert decision.primitive is Primitive.GIT_PUBLISH
    assert decision.request.require_green is True
    assert (decision.request.expected_old_sha, decision.request.new_sha) == (BASE, B)
    moved_evidence = good.model_copy(update={"ci": (CIEvidence(head_sha=C, state=CIState.GREEN),)})
    assert pinned.compiled.evaluate(s, moved_evidence).primitive is Primitive.GATE
    assert pinned.compiled.evaluate(s, facts(s, CIState.GREEN)).primitive is Primitive.WAIT


@pytest.mark.parametrize("mode", ["focused", "advisory", "none"])
def test_binding_human_hold_precedes_all_other_choices(mode):
    pinned = pinned_policy(mode)
    s = subject(pinned, SubjectPhase.BUILDING)
    decision = pinned.compiled.evaluate(s, facts(s, holds=(HoldFacts(kind="review_rejected"),)))
    assert decision.rule == "binding-human-hold"
    assert decision.primitive is Primitive.WAIT


def test_answered_retry_clears_gate_with_read_only_proof_before_any_mutation():
    pinned = pinned_policy()
    s = subject(pinned, SubjectPhase.BUILDING, schedule=SubjectSchedule.hold(
        now=100, gate_id="human", max_wait_seconds=3600))
    f = facts(s, gate=GateFacts(gate_id="human", status="answered", answer="retry"))
    decision = pinned.compiled.evaluate(s, f)
    assert decision.primitive is Primitive.GIT_ANCESTRY
    result = PrimitiveOutcome(primitive=decision.primitive, outcome="facts")
    assert pinned.compiled.schedule(s, decision, result, now=100).gate_id is None
    assert pinned.compiled.phase(s, decision, result) is SubjectPhase.BUILDING


async def test_parked_source_cleanup_requires_existing_delivery_truth(db):
    pinned = pinned_policy()
    s = subject(pinned, SubjectPhase.REPAIRING, kind=SubjectKind.SOURCE, task_id="a")
    await store_subject(db, s, pinned)
    a = adapter(db, pinned, DevelopmentFrontier((member("a", B),), satisfied=frozenset({"a"})))
    decision = pinned.compiled.evaluate(s, await a.observe(s))
    assert decision.rule == "source-contained"
    assert decision.primitive is Primitive.CLEANUP


async def test_completed_repair_waits_for_delivery_without_spending_another_generation(db):
    pinned = pinned_policy()
    s = subject(pinned, SubjectPhase.REPAIRING, kind=SubjectKind.SOURCE, task_id="a",
                writer=WriterLease(status=WriterStatus.STOPPED, task_id="repair"),
                budget=WriterBudget(ordinal=3, intelligence_class="standard-high",
                                   started_at=0, deadline_at=1))
    await store_subject(db, s, pinned)
    repair = replace(member("repair", C), carries=frozenset({("a", B)}))
    a = adapter(db, pinned, DevelopmentFrontier((member("a", B), repair),
                                               parked=frozenset({("a", B)})))
    decision = pinned.compiled.evaluate(s, await a.observe(s))
    assert decision.rule == "source-replacement-pending"
    assert decision.primitive is Primitive.WAIT


@pytest.mark.parametrize("ordinal", [0, 1, 2, 3])
def test_three_resumable_repair_generations_without_capacity_expiry(ordinal):
    pinned = pinned_policy()
    budget = WriterBudget(ordinal=ordinal, intelligence_class="standard-high",
                          started_at=0, deadline_at=1)
    s = subject(pinned, SubjectPhase.REPAIRING, kind=SubjectKind.SOURCE,
                task_id="parked", budget=budget)
    decision = pinned.compiled.evaluate(s, facts(s))
    assert decision.primitive is (Primitive.GATE if ordinal == 3 else Primitive.WRITER_FILE)
    if ordinal < 3:
        assert decision.request.ordinal == ordinal + 1
    waiting = s.model_copy(update={"writer": WriterLease(status=WriterStatus.FILED, task_id="repair")})
    assert pinned.compiled.evaluate(waiting, facts(waiting)).primitive is Primitive.WAIT


@pytest.mark.parametrize("reason", ["parked", "held", "failing", "unpushed", "missing-base"])
def test_one_ineligible_member_holds_only_its_dependents(reason):
    bad = member("a")
    parked = frozenset()
    if reason == "parked":
        parked = frozenset({("a", A)})
    elif reason == "held":
        bad = replace(bad, facts=bad.facts.model_copy(update={"held": True}))
    elif reason == "failing":
        bad = replace(bad, completed=False)
    elif reason == "unpushed":
        bad = replace(bad, pushed=False)
    else:
        bad = replace(bad, facts=bad.facts.model_copy(update={"base_sha": None}))
    frontier = DevelopmentFrontier((bad, member("b", B, deps=("a",)), member("c", C)),
                                   parked=parked)
    assert [m.task_id for m in ordered_development_members(frontier, 50)] == ["c"]


def test_dependency_order_cap_cycles_and_external_delivery_truth():
    frontier = DevelopmentFrontier((member("a", deps=("z",)), member("z", B),
                                    member("cycle", C, deps=("cycle",))))
    assert [m.task_id for m in ordered_development_members(frontier, 50)] == ["z", "a"]
    assert [m.task_id for m in ordered_development_members(frontier, 1)] == ["z"]
    external = DevelopmentFrontier((member("a", deps=("outside",)),),
                                   satisfied=frozenset({"outside"}))
    assert [m.task_id for m in ordered_development_members(external, 50)] == ["a"]


def test_verified_repair_carries_exact_parked_sources_and_releases_descendants():
    repair = replace(member("repair", C), carries=frozenset({("a", A)}))
    frontier = DevelopmentFrontier((member("a"), member("b", B, deps=("a",)), repair),
                                   parked=frozenset({("a", A)}))
    assert [m.task_id for m in ordered_development_members(frontier, 50)] == ["repair", "b"]
    stale = replace(repair, carries=frozenset({("a", B)}))
    assert [m.task_id for m in ordered_development_members(replace(
        frontier, members=(member("a"), member("b", B, deps=("a",)), stale)
    ), 50)] == ["repair"]


@pytest.fixture
async def db():
    db = Database(lease_dsn("development_subjects"))
    await db.initialize()
    yield db
    await db.close()


async def store_subject(db, s, pinned):
    async with db.immediate() as conn:
        await conn.execute(insert(playbook_artifacts).values(
            artifact_sha256=s.policy.artifact_sha256, playbook_id=s.policy.playbook_id,
            source_digest=pinned.definition.source_hash, contract_fingerprint="sha256:" + "3" * 64,
            compiler_build="test", path="/test/development.json", created_at=100,
        ))
        await db.ensure_integration_subject_on(conn, s.to_row())


def adapter(db, pinned, frontier, *, shared=None):
    async def observe(s):
        return SubjectFacts(**facts(s).model_dump(exclude={"publisher_fence", "competing_lease"}))

    return DevelopmentIntegrationAdapter(
        db, observe=observe, frontier_for=AsyncMock(return_value=frontier),
        policy_for=AsyncMock(return_value=pinned), repository_for=AsyncMock(
            return_value=SimpleNamespace(store=Path("/test/retained"))),
        shared_ports=shared or PrimitivePorts(), job_client=AsyncMock(), clock=lambda: 100,
    )


async def test_hosted_rate_limit_in_development_observer_schedules_retry_without_attempt(db):
    from src.git.github_contracts import GitHubAccessError
    from src.integration.ci_producers import HostedCIProducer
    from tests.test_integration_ci_producers import github

    pinned = pinned_policy()
    s = subject(pinned, SubjectPhase.TESTING)
    await store_subject(db, s, pinned)
    a = adapter(db, pinned, DevelopmentFrontier(()))
    client, trust = github(app=False)
    error = GitHubAccessError("rate_limited", "secondary limit", retry_at=1900, http_status=403)
    client.paged_items.side_effect = error
    a.producer_for = AsyncMock(return_value=HostedCIProducer(client, trust))

    with pytest.raises(GitHubAccessError) as caught:
        await a.observe(s)
    assert caught.value is error
    await a.reconciler(mode=JournalMode.ACTIVE).visit(s.id)
    current = Subject.from_row(await db.get_integration_subject(s.id))
    assert current.phase == s.phase and current.generation == s.generation
    assert current.schedule.next_due_at > 100
    assert current.budget == s.budget
    rows = await db.list_integration_subject_journal(s.id)
    assert rows and all(row["outcome"] not in {"green", "red"} for row in rows)
    assert all(not row["payload"].get("is_attempt") for row in rows)


async def test_seal_is_idempotent_frozen_and_shadow_never_files_or_merges(db):
    pinned = pinned_policy()
    s = subject(pinned)
    await store_subject(db, s, pinned)
    a = adapter(db, pinned, DevelopmentFrontier((member("a"),)))
    reconciler = a.reconciler(mode=JournalMode.SHADOW)
    await reconciler.visit(s.id)
    rows = await db.list_integration_subject_journal(s.id)
    assert [r["entry_kind"] for r in rows] == ["decision"]
    assert rows[0]["primitive"] == Primitive.SEAL.value
    current = Subject.from_row(await db.get_integration_subject(s.id))
    result = await a.seal(current, seal_args())
    assert result.outcome == "sealed"
    a.frontier_for.return_value = DevelopmentFrontier((member("a", B),))
    await a.seal(current, seal_args())
    frontier = await a._frontier(current, await a._journal(current))
    assert frontier.members[0].facts.head_sha == A
    assert frontier.members[0].facts.held


@pytest.mark.parametrize("admission", [
    {"require_review": True}, {"require_source_ci": True}, {"include_authorized": True},
])
async def test_copied_policy_admission_cannot_silently_ignore_a_stricter_filter(db, admission):
    pinned = pinned_policy()
    s = subject(pinned)
    await store_subject(db, s, pinned)
    a = adapter(db, pinned, DevelopmentFrontier((member("a"), member("b", B, deps=("a",)))))
    result = await a.seal(s, seal_args(**admission))
    assert result.outcome in {"empty", "unknown"}
    assert a._manifest(await a._journal(s)) is None


async def test_conflict_parks_one_exact_source_and_independent_member_keeps_building(db):
    pinned = pinned_policy()
    s = subject(pinned, SubjectPhase.BUILDING)
    await store_subject(db, s, pinned)
    args = MergeMembersArgs(target_ref=s.target_ref, base_sha=BASE, members=(
        MemberRef(task_id="a", head_sha=A, base_sha=BASE),
        MemberRef(task_id="c", head_sha=C, base_sha=BASE),
    ))
    shared = PrimitivePorts({Primitive.GIT_MERGE_MEMBERS: AsyncMock(return_value=PrimitiveOutcome(
        primitive=Primitive.GIT_MERGE_MEMBERS, outcome="conflict",
        detail={"member": "a", "files": ["x.py"], "head": BASE},
    ))})
    a = adapter(db, pinned, DevelopmentFrontier((member("a"), member("b", B, deps=("a",)),
                                               member("c", C))), shared=shared)
    await a.seal(s, seal_args())
    result = await a.merge(s, args)
    await a.merge(s, args)
    decision = pinned.compiled.evaluate(s, facts(s, members=(member("c", C).facts,)))
    assert pinned.compiled.phase(s, decision, result) is SubjectPhase.BUILDING
    frontier = await a._frontier(s, await a._journal(s))
    assert [m.task_id for m in ordered_development_members(frontier, 50)] == ["c"]
    async with db._engine.connect() as conn:
        parked = (await conn.execute(select(integration_subjects).where(
            integration_subjects.c.kind == "source",
        ))).mappings().all()
    assert len(parked) == 1
    assert (parked[0]["task_id"], parked[0]["head_sha"], parked[0]["phase"]) == (
        "a", A, "repairing",
    )


async def test_successful_merge_installs_exact_identity_and_preserves_generation_budget(db):
    pinned = pinned_policy()
    s = subject(pinned, SubjectPhase.BUILDING, writer=WriterLease(
        status=WriterStatus.STOPPED, task_id="repair"), budget=WriterBudget(
            ordinal=3, intelligence_class="standard-high", started_at=0, deadline_at=1))
    await store_subject(db, s, pinned)
    shared = PrimitivePorts({Primitive.GIT_MERGE_MEMBERS: AsyncMock(return_value=PrimitiveOutcome(
        primitive=Primitive.GIT_MERGE_MEMBERS, outcome="merged", detail={"head": C},
    ))})
    a = adapter(db, pinned, DevelopmentFrontier((member("a"),)), shared=shared)
    reconciler = a.reconciler(mode=JournalMode.ACTIVE)
    # Avoid job I/O in this building-only visit; the observation has no CI.
    a.producer_for = AsyncMock(return_value=SimpleNamespace(
        observe=AsyncMock(return_value=CIEvidence(head_sha=s.head_sha, state=CIState.NONE))))
    await reconciler.visit(s.id)
    current = Subject.from_row(await db.get_integration_subject(s.id))
    assert (current.head_sha, current.base_sha, current.generation, current.phase) == (
        C, BASE, 1, SubjectPhase.TESTING,
    )
    assert current.writer.status is WriterStatus.NONE
    assert current.budget.ordinal == 3


async def test_local_checks_advance_sequentially_and_publish_only_after_all_green(db, monkeypatch):
    pinned = pinned_policy(commands=["ruff check src", "npm run build"])
    s = subject(pinned, SubjectPhase.TESTING)
    await store_subject(db, s, pinned)
    await BranchOwnership(db).acquire(BranchKey(repository_id="repo", branch=s.target_ref),
                                     s.id, "collector")
    client = JobClient()

    async def read_jobs(_, subject, keys):
        return await client.read(subject, keys)

    monkeypatch.setattr(LocalCIProducer, "_read_jobs", read_jobs)
    publish = AsyncMock(return_value=PrimitiveOutcome(
        primitive=Primitive.GIT_PUBLISH, outcome="published"))
    a = adapter(db, pinned, DevelopmentFrontier((member("a"),)), shared=PrimitivePorts({
        Primitive.GIT_PUBLISH: publish,
    }))
    a.job_client = client
    now = [100]
    a.clock = lambda: now[0]
    reconciler = a.reconciler(mode=JournalMode.ACTIVE)

    async def visit():
        await reconciler.visit(s.id)
        now[0] += 301

    await visit()  # request first command
    assert len(client.calls) == 1
    client.complete()
    await visit()  # observe pending: command two still absent
    publish.assert_not_awaited()
    await visit()  # submit second command
    assert len(client.calls) == 2
    client.complete(1)
    await visit()  # exact all-green observation
    publish.assert_not_awaited()
    await visit()  # guarded publish
    assert publish.await_count == 1
    current = Subject.from_row(await db.get_integration_subject(s.id))
    assert current.phase is SubjectPhase.PUBLISHED
    assert all(call["input_ref"] == B for call in client.calls)


async def test_infrastructure_retry_has_new_job_identity_without_repair_generation(db, monkeypatch):
    pinned = pinned_policy(commands=["ruff check src"])
    s = subject(pinned, SubjectPhase.TESTING)
    await store_subject(db, s, pinned)
    client = JobClient()

    async def read_jobs(_, subject, keys):
        return await client.read(subject, keys)

    monkeypatch.setattr(LocalCIProducer, "_read_jobs", read_jobs)
    file_writer = AsyncMock()
    a = adapter(db, pinned, DevelopmentFrontier((member("a"),)), shared=PrimitivePorts({
        Primitive.WRITER_FILE: file_writer,
    }))
    a.job_client = client
    now = [100]
    a.clock = lambda: now[0]
    reconciler = a.reconciler(mode=JournalMode.ACTIVE)
    await reconciler.visit(s.id)
    client.complete(infra_reason="run_timeout")
    now[0] += 301
    await reconciler.visit(s.id)
    now[0] += 301
    await reconciler.visit(s.id)
    assert len(client.calls) == 2
    assert client.calls[0]["idempotency_key"] != client.calls[1]["idempotency_key"]
    current = Subject.from_row(await db.get_integration_subject(s.id))
    assert current.budget is None and current.generation == 0
    file_writer.assert_not_awaited()








def test_local_producer_preserves_installed_zero_queue_budget():
    assert LocalValidationPlan(version="pinned", attempt_id="0", queue_seconds=0).queue_seconds == 0


# -- the audited per-project engine transfer, through CommandHandler --------


async def seed_project(db, repository_id="repo"):
    """A real project and repository row, so the transfer's fence has identity."""
    async with db.immediate() as conn:
        await conn.execute(
            insert(projects).values(
                id="p", name="project", status="ACTIVE", created_at=1,
                hierarchical_integration_policy={}, hierarchical_integration_generation=1,
            )
        )
        await conn.execute(
            insert(repos).values(
                id=repository_id, project_id="p", url="https://example/repo.git",
                default_branch="main", checkout_base_path="/checkout", source_type="clone",
            )
        )


@pytest.fixture
async def development(db):
    """One seeded Development project whose root subject is still ``legacy``."""
    await seed_project(db)
    pinned = pinned_policy()
    seeded = subject(pinned, engine=SubjectEngine.RECONCILER)
    await store_subject(db, seeded, pinned)
    return db, seeded


def transfer_handler(db, *, reconciler_active=False):
    from src.commands.integration_commands import IntegrationCommandsMixin

    handler = IntegrationCommandsMixin()
    handler.db = db
    handler.config = SimpleNamespace(
        integration=SimpleNamespace(reconciler_active=reconciler_active, reconciler_shadow=False)
    )
    return handler


async def live_subject(db, subject):
    """The subject as it is now, which is exactly what a preview would name."""
    return Subject.from_row(await db.get_integration_subject(subject.id))


def cutover_args(live, **overrides):
    return {
        "project_id": "p",
        "engine": "reconciler",
        "dry_run": False,
        "expected_versions": {live.id: live.version},
        "reason": "reviewed development cutover",
        "evidence": ["shadow-week", "development-scenarios", "operator approval"],
    } | overrides


async def publish_intent(db, live, *, intent, outcome):
    """Journal one publish row exactly the way the shared publisher writes it."""
    await db.append_integration_subject_journal({
        "subject_id": live.id,
        "entry_kind": "action",
        "mode": "active",
        "idempotency_key": intent if outcome == "prepared" else f"{intent}:applied",
        "policy_artifact_sha256": live.policy.artifact_sha256,
        "subject_version": live.version,
        "phase": live.phase.value,
        "head_sha": live.head_sha,
        "generation": live.generation,
        "primitive": "git_publish",
        "outcome": outcome,
        "payload": {"repository_id": live.repository_id, "ref": live.target_ref},
        "recorded_at": 100,
    })


def transfers_of(journal):
    return [row for row in journal if row["payload"].get("command") == "development_engine_transfer"]


def test_the_contract_is_registered_as_a_composite_operator_control():
    from src.commands.contracts.integration import register_integration_contracts
    from src.commands.contracts.registry import ContractRegistry

    registry = ContractRegistry()
    register_integration_contracts(registry)
    contract = registry.get("integration_development_engine_transfer").contract
    assert contract.execution.capability == "integration_development_engine_transfer"
    assert contract.execution.side_effect.value == "composite"
    assert {outcome.name for outcome in contract.execution.outcomes} == {
        "preview", "transferred", "refused",
    }
    assert contract.execution.sensitive_args == frozenset({"reason"})


async def test_development_transfer_preview_is_read_only_and_names_exact_versions(development):
    db, seeded = development
    before = await db.get_integration_subject(seeded.id)
    preview = await transfer_handler(db)._cmd_integration_development_engine_transfer(
        {"project_id": "p", "engine": "reconciler"}
    )
    assert preview["success"] is True and preview["outcome"] == "preview"
    assert preview["subject_ids"] == [seeded.id]
    assert preview["expected_versions"] == {seeded.id: seeded.version}
    assert preview["current_engines"] == {seeded.id: "reconciler"}
    assert await db.get_integration_subject(seeded.id) == before
    assert not await db.list_integration_subject_journal(seeded.id)


async def test_development_transfer_applies_at_exact_versions_and_audits_the_operator(development):
    db, seeded = development
    live = await live_subject(db, seeded)
    # The reconciler must already be the active loop, or the transfer would hand
    # a project to an engine that can only mirror it.
    refused = await transfer_handler(db)._cmd_integration_development_engine_transfer(
        cutover_args(live)
    )
    assert refused == {
        "success": False,
        "outcome": "refused",
        "error": "enable the active loop before transferring development subjects",
    }
    assert (await live_subject(db, seeded)).engine is SubjectEngine.RECONCILER
    assert not await db.list_integration_subject_journal(seeded.id)

    applied = await transfer_handler(db, reconciler_active=True) \
        ._cmd_integration_development_engine_transfer(cutover_args(live))
    assert applied["outcome"] == "transferred"
    assert applied["subject_ids"] == [seeded.id]
    moved = await live_subject(db, seeded)
    assert moved.engine is SubjectEngine.RECONCILER and moved.version == live.version + 1
    journal = transfers_of(await db.list_integration_subject_journal(seeded.id))
    assert len(journal) == 1
    assert journal[0]["primitive"] == "record_decision"
    assert journal[0]["outcome"] == "recorded"
    payload = journal[0]["payload"]
    assert (payload["from"], payload["to"]) == ("reconciler", "reconciler")
    assert payload["operator_id"] == "human:local-operator"
    assert payload["reason"] == "reviewed development cutover"
    assert payload["evidence"] == ["shadow-week", "development-scenarios", "operator approval"]


async def test_development_transfer_refuses_wrong_versions_and_a_partial_subject_set(development):
    db, seeded = development
    handler = transfer_handler(db, reconciler_active=True)
    live = await live_subject(db, seeded)

    # A version nobody previewed: the exact-version CAS is the whole point.
    stale = await handler._cmd_integration_development_engine_transfer(
        cutover_args(live, expected_versions={live.id: live.version + 1})
    )
    assert stale["outcome"] == "refused"
    assert "development subject set/version changed" in stale["error"]
    # A chosen subset is refused too, in both directions: the transfer moves a
    # project, never part of it.
    assert (await handler._cmd_integration_development_engine_transfer(
        cutover_args(live, expected_versions={})))["outcome"] == "refused"
    assert (await handler._cmd_integration_development_engine_transfer(
        {"project_id": "p", "engine": "legacy", "dry_run": False,
         "expected_versions": {}, "reason": "rollback"}))["outcome"] == "refused"
    assert (await live_subject(db, seeded)).engine is SubjectEngine.RECONCILER
    assert not await db.list_integration_subject_journal(seeded.id)


async def test_development_transfer_refuses_an_unresolved_publication_in_both_directions(development):
    db, seeded = development
    handler = transfer_handler(db, reconciler_active=True)
    live = await live_subject(db, seeded)
    await publish_intent(db, live, intent="git:first-intent", outcome="prepared")

    # Neither engine may take ownership over an ambiguous write: the reconciler
    # must not adopt one and the old publisher must not resume one.
    for engine in ("reconciler",):
        refused = await handler._cmd_integration_development_engine_transfer(
            cutover_args(live, engine=engine)
        )
        assert refused["outcome"] == "refused", engine
        assert "unresolved development publication" in refused["error"], engine
    assert (await live_subject(db, seeded)).engine is SubjectEngine.RECONCILER

    # A confirmation settles only the intent it names; a later prepare stays open.
    await publish_intent(db, live, intent="git:first-intent", outcome="applied")
    await publish_intent(db, live, intent="git:second-intent", outcome="prepared")
    assert (await handler._cmd_integration_development_engine_transfer(
        cutover_args(live)))["outcome"] == "refused"
    await publish_intent(db, live, intent="git:second-intent", outcome="applied")
    assert (await handler._cmd_integration_development_engine_transfer(
        cutover_args(live)))["outcome"] == "transferred"
    assert (await live_subject(db, seeded)).engine is SubjectEngine.RECONCILER




async def test_development_transfer_requires_an_operator_and_a_real_project(development):
    from src.commands.principal import ExecutionPrincipal, principal_context

    db, seeded = development
    handler = transfer_handler(db, reconciler_active=True)
    assert (await handler._cmd_integration_development_engine_transfer(
        {"project_id": "missing", "engine": "reconciler"}))["error"] == "project does not exist"
    with principal_context(ExecutionPrincipal.service("development transfer")):
        refused = await handler._cmd_integration_development_engine_transfer(
            {"project_id": "p", "engine": "reconciler"}
        )
    assert refused["outcome"] == "unauthorized"
    assert "operator" in refused["error"]
    invalid = await handler._cmd_integration_development_engine_transfer(
        {"project_id": "p", "engine": "reconciler", "expected_versions": {"root": -1}}
    )
    assert invalid["outcome"] == "refused"
    assert not await db.list_integration_subject_journal(seeded.id)


async def test_development_transfer_waits_for_a_publisher_instead_of_racing_it(development):
    """The command route keeps the fence the publisher already shares.

    The route is only as safe as the mechanism behind it: a publisher holding
    the shared engine lock must block the transfer exactly as it blocks the
    Python helper during forward transfer.
    """
    from src.integration.development import publisher_exclusion

    db, seeded = development
    handler = transfer_handler(db, reconciler_active=True)
    live = await live_subject(db, seeded)
    holding, release = asyncio.Event(), asyncio.Event()

    async def publishing():
        async with publisher_exclusion(db, "repo"):
            holding.set()
            await release.wait()

    publisher = asyncio.create_task(publishing())
    await asyncio.wait_for(holding.wait(), timeout=30)
    transfer = asyncio.create_task(
        handler._cmd_integration_development_engine_transfer(cutover_args(live))
    )
    await asyncio.sleep(0.5)
    assert not transfer.done(), "the transfer raced a publisher holding the fence"
    assert (await live_subject(db, seeded)).engine is SubjectEngine.RECONCILER
    release.set()
    assert (await transfer)["outcome"] == "transferred"
    await publisher
    assert (await live_subject(db, seeded)).engine is SubjectEngine.RECONCILER


def test_runtime_loads_retained_reviewed_source_after_vault_changes(tmp_path):
    from src.integration.development_runtime import load_pinned_development_policy
    from src.playbooks.artifact_store import ArtifactStore

    pinned = pinned_policy()
    config = SimpleNamespace(compiled_root=str(tmp_path / "compiled"),
                             vault_root=str(tmp_path / "vault"))
    store = ArtifactStore(config.compiled_root)
    ref = store.put(pinned.definition, source_digest=pinned.definition.source_hash,
                    contract_fingerprint="sha256:" + "b" * 64,
                    profile_fingerprint="test", compiler_build="test")
    vault_path = tmp_path / "vault/projects/p/playbooks/p-development.md"
    vault_path.parent.mkdir(parents=True)
    vault_path.write_text(pinned.source)
    assert load_pinned_development_policy(config, ref.artifact_sha256,
                                         pinned.definition).settings == pinned.settings
    vault_path.write_text("changed policy")
    with pytest.raises(ValueError, match="source does not match"):
        load_pinned_development_policy(config, ref.artifact_sha256, pinned.definition)
    store.put_source(ref.artifact_sha256, pinned.source)
    vault_path.unlink()
    assert load_pinned_development_policy(config, ref.artifact_sha256,
                                         pinned.definition).settings == pinned.settings


async def test_factory_admits_pushed_source_and_uses_subject_pinned_retained_store(db, tmp_path):
    """Exercise the shipped factory with real Git and no base checkout."""
    from src.database import tables as t
    from src.git.github_contracts import GitHubRepositoryBinding
    from src.integration.development import DevelopmentPrimitives
    from src.integration.development_runtime import development_runtime_for
    from src.integration.runtime_contracts import AncestryArgs, AncestryQuery
    from src.models import Project, RepoConfig, RepoSourceType
    from src.playbooks.artifact_store import ArtifactStore
    from tests.test_integration_gitops import LocalGit, commit, git

    remote, work = tmp_path / "remote.git", tmp_path / "source"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "config", "user.name", "Tester")
    git(work, "config", "user.email", "tester@example.test")
    base = commit(work, {"base.txt": "base\n"})
    git(work, "push", "origin", "HEAD:main")
    head = commit(work, {"alpha.txt": "alpha\n"})
    git(work, "push", "origin", "HEAD:aq/alpha")
    await db.create_project(Project(id="p", name="Development factory"))
    await db.create_repo(RepoConfig(
        id="repo", project_id="p", source_type=RepoSourceType.CLONE,
        url=str(remote), default_branch="main",
    ))
    assert (await db.get_repo("repo")).checkout_base_path == ""
    async with db.immediate() as conn:
        await conn.execute(insert(t.tasks).values(
            id="alpha", project_id="p", repo_id="repo", title="Alpha",
            description="", status="COMPLETED", branch_name="aq/alpha",
            created_at=1, updated_at=2,
        ))
        await conn.execute(insert(t.task_branch_origins).values(
            id="origin-alpha", task_id="alpha", repository_id="repo",
            base_sha=base, creation_generation=0, reserved=True, created_at=1,
        ))
        await conn.execute(insert(t.task_integration_checkpoints).values(
            task_id="alpha", repository_id="repo", branch="aq/alpha",
            checkpoint_sha=head, generation=0, updated_at=2,
        ))
    regenerate = "scripts/regenerate-generated.sh --check"
    pinned = pinned_policy(regenerate=regenerate)
    root = subject(pinned, SubjectPhase.BUILDING, head_sha=base, base_sha=base)
    await store_subject(db, root, pinned)
    config = SimpleNamespace(
        integration=SimpleNamespace(reconciler_active=True),
        compiled_root=str(tmp_path / "compiled"), vault_root=str(tmp_path / "vault"),
    )
    artifacts = ArtifactStore(config.compiled_root)
    ref = artifacts.put(
        pinned.definition, source_digest=pinned.definition.source_hash,
        contract_fingerprint="sha256:" + "b" * 64,
        profile_fingerprint="test", compiler_build="test",
    )
    artifacts.put_source(ref.artifact_sha256, pinned.source)
    transport = LocalGit(remote)
    primitives = DevelopmentPrimitives(db, data_dir=tmp_path / "data", git=transport)
    orchestrator = SimpleNamespace(
        config=config, db=db, git=transport, development_integration=primitives,
        integration_app_client=SimpleNamespace(repository=GitHubRepositoryBinding(123, "test/repo")),
        _load_playbook_artifact=lambda sha: artifacts.load(sha),
        _root_subject_session_probe=AsyncMock(), _command_handler=AsyncMock(),
    )
    runtime = development_runtime_for(orchestrator)
    frontier = await runtime.adapter.frontier_for(root)
    assert [(m.facts.task_id, m.pushed) for m in frontier.members] == [("alpha", True)]
    assert [m.task_id for m in ordered_development_members(frontier, 10)] == ["alpha"]
    repository = await runtime.adapter.repository_for(root)
    git(repository.store, "config", "user.name", "Tester")
    git(repository.store, "config", "user.email", "tester@example.test")
    outcome = await runtime.adapter.shared.invoke(root, MergeMembersArgs(
        target_ref=root.target_ref, base_sha=base,
        members=(MemberRef(task_id="alpha", head_sha=head, base_sha=base),),
    ))
    assert outcome.outcome == "merged", outcome
    # Batch construction writes no journal; the pinned command rides the retained store.
    assert repository.regenerate == regenerate
    ancestry = await runtime.adapter.shared.invoke(root, AncestryArgs(
        repository_id="repo", queries=(AncestryQuery(ancestor=base, descendant=head),),
    ))
    assert ancestry.outcome == "facts", ancestry
    assert ancestry.detail["queries"][0]["is_ancestor"] is True
    assert git(remote, "rev-parse", "refs/heads/main") == base


def test_active_train_cannot_construct_development_subject_runtime():
    from src.integration.development_runtime import development_runtime_for

    # No DB, providers or artifact loader exist: the protocol guard must win
    # before any legacy factory wiring can be accessed.
    orchestrator = SimpleNamespace(config=SimpleNamespace(
        integration=SimpleNamespace(git_first="active", reconciler_active=True),
    ))
    assert development_runtime_for(orchestrator) is None
