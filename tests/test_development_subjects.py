"""Retained reviewed Development policy and exact-head validation contracts."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from src.database.tables import playbook_artifacts

from src.integration.ci_producers import LocalValidationPlan
from src.integration.development import DevelopmentPolicy
from src.integration.development_policy import PinnedDevelopmentPolicy, render_development_policy
from src.integration.models import BranchKey, Fence
from src.integration.runtime_contracts import (
    CIEvidence, CIState, HoldFacts, GateFacts, MemberFacts, Primitive, PrimitiveOutcome, Subject,
    SubjectEngine, SubjectKind, SubjectPhase, SubjectSchedule, WriterBudget, WriterLease, WriterStatus,
)
from src.playbooks.definition import ProjectScope, load_definition_json, source_digest
from src.playbooks.integration_policy import IntegrationPolicyFacts, policy_from_markdown

BASE, A, B, C = (letter * 40 for letter in "abcd")


def pinned_policy(mode="focused", **settings):
    settings.setdefault("commands", ["aq test tests/test_development_validation.py"])
    source = render_development_policy("p", DevelopmentPolicy(validation=mode, **settings))
    artifact = load_definition_json(Path(
        "tests/fixtures/playbooks/historical-v2/root-train/artifact.json"
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


async def store_subject(db, s, pinned):
    """Persist historical subjects for retained cleanup-fence tests."""
    async with db.immediate() as conn:
        await conn.execute(insert(playbook_artifacts).values(
            artifact_sha256=s.policy.artifact_sha256, playbook_id=s.policy.playbook_id,
            source_digest=pinned.definition.source_hash, contract_fingerprint="sha256:" + "3" * 64,
            compiler_build="test", path="/test/development.json", created_at=100,
        ))
        await db.ensure_integration_subject_on(conn, s.to_row())


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


def test_local_producer_preserves_installed_zero_queue_budget():
    assert LocalValidationPlan(version="pinned", attempt_id="0", queue_seconds=0).queue_seconds == 0


def test_runtime_loads_retained_reviewed_source_after_vault_changes(tmp_path):
    from src.integration.development_policy import load_pinned_development_policy
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
