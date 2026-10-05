"""Reviewed subject tables: compiler guards, pure decisions and immutable pins."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.integration.models import BranchKey, Fence
from src.integration.subjects import (
    CIEvidence,
    CIState,
    GateArgs,
    HoldFacts,
    MemberFacts,
    PolicyArtifactPin,
    Primitive,
    PrimitiveOutcome,
    Subject,
    SubjectFacts,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
    UnresolvedWrite,
    WriterBudget,
    WriterLease,
    WriterStatus,
)
from src.playbooks.artifact_diff import diff_artifacts
from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import canonical_bytes, load_definition_json
from src.playbooks.integration_policy import (
    CompiledIntegrationPolicy,
    IntegrationPolicy,
    IntegrationPolicyFacts,
    policy_from_markdown,
)
from src.playbooks.proposal import propose
from src.playbooks.semantic_diff import diff_definitions
from src.playbooks.validation import (
    NullProfileLookup,
    RegisteredEventLookup,
    RegistryContractLookup,
)
from tests.playbook_v2_engine_helpers import artifact_ref_for

BUNDLE = Path("tests/fixtures/playbooks/v2/agent-queue-root-train")
TEMPLATE = Path("tests/fixtures/playbooks/v2/root-train")
BASE = "a" * 40
HEAD = "b" * 40


@pytest.fixture
def artifact():
    return load_definition_json((BUNDLE / "artifact.json").read_text())


@pytest.fixture
def raw_policy(artifact):
    return artifact.integration_policy.model_dump(mode="json", exclude_none=True)


def make_subject(artifact, phase=SubjectPhase.ADMITTING, **changes):
    values = dict(
        id="root",
        project_id="agent-queue",
        repository_id="repo",
        kind=SubjectKind.ROOT_BATCH,
        subject_key="root_batch:repo:request",
        phase=phase,
        policy=PolicyArtifactPin(
            playbook_id=artifact.id, artifact_sha256=artifact.artifact_sha256()
        ),
        target_ref="main",
        base_sha=BASE,
        head_sha=HEAD,
        schedule=SubjectSchedule.progress(now=100, max_wait_seconds=3600),
        created_at=100,
        updated_at=100,
    )
    values.update(changes)
    return Subject(**values)


def make_facts(subject, **changes):
    values = dict(
        subject_id=subject.id,
        subject_version=subject.version,
        kind=subject.kind,
        phase=subject.phase,
        observed_at=200,
        head=subject.head,
        default_branch_head=BASE,
        writer=subject.writer,
        budget=subject.budget,
        members=(MemberFacts(task_id="source", head_sha=HEAD, base_sha=BASE, review="approved"),),
    )
    values.update(changes)
    return IntegrationPolicyFacts(**values)


def outcome(decision, value, **detail):
    return PrimitiveOutcome(primitive=decision.primitive, outcome=value, detail=detail)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("outcome", "closed outcomes"),
        ("missing_unknown", "closed outcomes"),
        ("unbounded_wait", "finite seconds bound"),
        ("overlong_wait", "max_wait_seconds"),
        ("gate_without_default", "default choice and timeout"),
        ("gate_bad_default", "one of the gate"),
        ("gate_ignores_hold", "must hold"),
        ("gate_missing_answer", "every declared answer"),
        ("bad_literal_input", "valid boolean"),
        ("no_overdue", "wait_overdue"),
        ("overdue_false", "wait_overdue"),
        ("unknown_binding", "unknown policy binding"),
        ("unknown_path", "unknown policy path"),
        ("missing_argument", "inputs: missing"),
        ("unbounded_default", "default must be"),
        ("human_hold_after_mutation", "first case"),
    ],
)
def test_malformed_tables_rejected(raw_policy, mutation, match):
    raw = copy.deepcopy(raw_policy)
    table = raw["tables"]["root_batch"]
    actions = table["actions"]
    if mutation == "outcome":
        actions["build"]["outcomes"]["teleported"] = {"kind": "progress"}
    elif mutation == "missing_unknown":
        del actions["build"]["outcomes"]["unknown"]
    elif mutation == "unbounded_wait":
        del actions["wait"]["outcomes"]["waiting"]["seconds"]
    elif mutation == "overlong_wait":
        actions["wait"]["inputs"]["seconds"]["value"] = 3601
        actions["wait"]["outcomes"]["waiting"]["seconds"] = 3601
    elif mutation == "gate_without_default":
        actions["exhaustion-gate"]["inputs"]["no_default"]["value"] = False
    elif mutation == "gate_bad_default":
        inputs = actions["exhaustion-gate"]["inputs"]
        inputs["no_default"]["value"] = False
        inputs["default_choice"] = {"type": "literal", "value": "eject"}
        inputs["default_after_seconds"] = {"type": "literal", "value": 120}
    elif mutation == "gate_ignores_hold":
        actions["exhaustion-gate"]["outcomes"]["created"] = {"kind": "progress"}
    elif mutation == "gate_missing_answer":
        del actions["exhaustion-gate"]["answers"]["hold"]
    elif mutation == "bad_literal_input":
        actions["build"]["inputs"]["regenerate_generated"]["value"] = "invented"
    elif mutation == "no_overdue":
        table["cases"] = [case for case in table["cases"] if case["rule"] != "overdue-wait"]
    elif mutation == "overdue_false":
        table["cases"][-1]["when"]["right"]["value"] = False
    elif mutation == "unknown_binding":
        case = next(case for case in table["cases"] if case["rule"] == "unknown-facts")
        case["when"]["value"]["binding"] = "invented"
    elif mutation == "unknown_path":
        case = next(case for case in table["cases"] if case["rule"] == "unknown-facts")
        case["when"]["value"]["path"] = "invented"
    elif mutation == "missing_argument":
        del actions["build"]["inputs"]["members"]
    elif mutation == "unbounded_default":
        table["default"] = "build"
    elif mutation == "human_hold_after_mutation":
        table["cases"][0], table["cases"][1] = table["cases"][1], table["cases"][0]
    with pytest.raises(ValidationError, match=match):
        IntegrationPolicy.model_validate(raw)


def test_source_block_rejects_duplicates_and_unterminated(raw_policy):
    with pytest.raises(ValueError, match="duplicate"):
        policy_from_markdown('```integration-policy\n{"tables": {}, "tables": {}}\n```')
    with pytest.raises(ValueError, match="unterminated"):
        policy_from_markdown("```integration-policy\n{}")
    valid = "```integration-policy\n" + json.dumps(raw_policy) + "\n```\n"
    with pytest.raises(ValueError, match="unterminated"):
        policy_from_markdown(valid + "```integration-policy\n{}")


def test_compiler_reads_author_block_and_rejects_invented_table(artifact, tmp_path):
    source_path = tmp_path / "source.md"
    source_path.write_text((BUNDLE / "source.md").read_text())
    source = PlaybookSource.load(source_path, vault_root=tmp_path)
    body = {name: artifact.model_dump(mode="json")[name] for name in ("rules", "steps")}
    lookups = dict(
        contracts=RegistryContractLookup(),
        profiles=NullProfileLookup(),
        events=RegisteredEventLookup(),
        version=2,
    )
    proposal = propose(source, body, **lookups)
    assert proposal.activatable
    assert proposal.artifact.integration_policy == artifact.integration_policy
    body["integration_policy"] = {"tables": {}}
    rejected = propose(source, body, **lookups)
    assert not rejected.activatable
    assert rejected.artifact is None
    assert "differs from the authored block" in rejected.diagnostics[-1].message


def test_admission_and_holds_are_table_decisions(artifact):
    subject = make_subject(artifact)
    policy = CompiledIntegrationPolicy(artifact)
    decision = policy.evaluate(subject, make_facts(subject))
    assert decision.primitive == Primitive.SEAL
    assert decision.request.admission.include_authorized
    assert decision.request.admission.require_source_ci
    assert decision.request.admission.require_review
    assert decision.request.admission.task_kinds == ("feature", "bugfix")
    held = policy.evaluate(subject, make_facts(subject, holds=(HoldFacts(kind="manual_pause"),)))
    assert held.rule == "binding-human-hold"
    assert held.primitive == Primitive.WAIT
    schedule = policy.schedule(subject, held, outcome(held, "waiting"), now=200)
    assert schedule.next_due_at == 260
    assert policy.phase(subject, held, outcome(held, "waiting")) is None


def test_unknown_and_overdue_are_bounded_decisions(artifact):
    policy = CompiledIntegrationPolicy(artifact)
    subject = make_subject(artifact, SubjectPhase.PUBLISHING)
    decision = policy.evaluate(subject, make_facts(subject, wait_overdue=True))
    assert decision.rule == "overdue-wait"
    schedule = policy.schedule(subject, decision, outcome(decision, "waiting"), now=200)
    assert schedule.next_due_at <= 200 + subject.schedule.max_wait_seconds
    unknown = policy.evaluate(subject, make_facts(subject, unknown=("remote-unavailable",)))
    assert unknown.rule == "unknown-facts"
    refused = PrimitiveOutcome.unknown(unknown.primitive, "remote-unavailable")
    backed_off = policy.schedule(subject, unknown, refused, now=200)
    assert backed_off.next_due_at == 230
    assert backed_off.refusal_streak == 1


def test_empty_frontier_waits_and_empty_seal_closes(artifact):
    policy = CompiledIntegrationPolicy(artifact)
    subject = make_subject(artifact)
    empty_frontier = policy.evaluate(subject, make_facts(subject, members=()))
    assert empty_frontier.rule == "admit-empty"
    assert empty_frontier.request.seconds == 300
    sealed = policy.evaluate(subject, make_facts(subject))
    schedule = policy.schedule(subject, sealed, outcome(sealed, "empty"), now=200)
    assert schedule.closed_reason == "empty-frontier"
    assert policy.phase(subject, sealed, outcome(sealed, "empty")) == SubjectPhase.DONE


def test_unclaimed_queue_time_is_not_repair_failure(artifact):
    budget = WriterBudget(
        ordinal=0,
        intelligence_class="standard-high",
        started_at=100,
        deadline_at=150,
        attempt_limit=3,
    )
    subject = make_subject(
        artifact,
        SubjectPhase.REPAIRING,
        writer=WriterLease(status=WriterStatus.FILED, task_id="repair"),
        budget=budget,
    )
    decision = CompiledIntegrationPolicy(artifact).evaluate(subject, make_facts(subject))
    assert decision.rule == "writer-unclaimed-expired"
    assert decision.primitive == Primitive.WAIT
    assert decision.request.seconds == 1800
    assert decision.messages


def test_a_table_can_eject_the_earliest_conflicting_member_by_derived_path(raw_policy, artifact):
    from src.integration.subjects import ConflictFacts, EjectArgs

    table = copy.deepcopy(raw_policy)
    root = table["tables"]["root_batch"]
    case = next(case for case in root["cases"] if case["rule"] == "writer-unclaimed-expired")
    case["action"] = "eject-unclaimed"
    del root["actions"]["capacity"]
    root["actions"]["eject-unclaimed"] = {
        "primitive": "eject",
        "inputs": {
            "member_task_id": {"type": "binding_ref", "binding": "s", "path": "conflict_member"},
            "reason": {"type": "literal", "value": "writer-unclaimed-after-budget"},
        },
        "outcomes": {
            "ejected": {"kind": "progress", "phase": "building"},
            "not_a_member": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600},
            "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600},
        },
    }
    variant = artifact.model_copy(
        update={"integration_policy": IntegrationPolicy.model_validate(table)}
    )
    subject = make_subject(
        variant,
        SubjectPhase.REPAIRING,
        writer=WriterLease(status=WriterStatus.FILED, task_id="repair"),
        budget=WriterBudget(
            ordinal=0, intelligence_class="standard-high", started_at=100, deadline_at=150,
            attempt_limit=3,
        ),
    )
    members = tuple(
        MemberFacts(task_id=name, head_sha=HEAD, base_sha=BASE, review="approved")
        for name in ("alpha", "bravo", "charlie")
    )
    # Conflicts arrive unordered; the manifest order chooses the member.
    conflicts = (ConflictFacts(member_task_id="charlie"), ConflictFacts(member_task_id="bravo"))
    decision = CompiledIntegrationPolicy(variant).evaluate(
        subject, make_facts(subject, members=members, conflicts=conflicts)
    )
    assert decision.request == EjectArgs(
        member_task_id="bravo", reason="writer-unclaimed-after-budget"
    )
    # Without a conflict the line has no member to name; it is never guessed.
    with pytest.raises(ValueError):
        CompiledIntegrationPolicy(variant).evaluate(subject, make_facts(subject, members=members))


def test_green_decision_preserves_exact_expected_old_fence(artifact):
    subject = make_subject(artifact, SubjectPhase.PROMOTABLE)
    fence = Fence(
        target=BranchKey(repository_id="repo", branch="main"), owner_id="publisher", token=7
    )
    facts = make_facts(
        subject, ci=(CIEvidence(head_sha=HEAD, state=CIState.GREEN),), publisher_fence=fence
    )
    decision = CompiledIntegrationPolicy(artifact).evaluate(subject, facts)
    assert decision.primitive == Primitive.GIT_PUBLISH
    assert decision.request.new_sha == HEAD
    assert decision.request.expected_old_sha == BASE
    assert decision.request.require_green
    assert decision.request.fence.token == 7
    assert decision.request.fence.owner_id == "publisher"
    red = facts.model_copy(update={"ci": (CIEvidence(head_sha=HEAD, state=CIState.RED),)})
    assert (
        CompiledIntegrationPolicy(artifact).evaluate(subject, red).primitive
        != Primitive.GIT_PUBLISH
    )


def test_stop_proof_uses_the_observed_writer(artifact):
    subject = make_subject(artifact, SubjectPhase.REPAIRING)
    facts = make_facts(
        subject,
        writer=WriterLease(status=WriterStatus.WORKING, task_id="observed-repair", fence_token=11),
        budget=WriterBudget(
            ordinal=0, intelligence_class="standard-high", started_at=100, deadline_at=150
        ),
    )
    decision = CompiledIntegrationPolicy(artifact).evaluate(subject, facts)
    assert decision.primitive == Primitive.WRITER_STOP_PROOF
    assert decision.request.task_id == "observed-repair"
    assert decision.request.fence_token == 11


def test_publication_never_infers_publisher_fence_from_repair_writer(artifact):
    writer = WriterLease(status=WriterStatus.WORKING, task_id="repair", fence_token=7)
    subject = make_subject(artifact, SubjectPhase.PROMOTABLE, writer=writer)
    facts = make_facts(subject, ci=(CIEvidence(head_sha=HEAD, state=CIState.GREEN),))
    plain = SubjectFacts.model_validate(
        facts.model_dump(exclude={"publisher_fence", "competing_lease"})
    )
    decision = CompiledIntegrationPolicy(artifact).evaluate(subject, plain)
    assert decision.rule == "publisher-fence-unavailable"
    assert decision.primitive == Primitive.WAIT
    bad_fence = Fence(
        target=BranchKey(repository_id="other", branch="main"), owner_id="publisher", token=7
    )
    with pytest.raises(ValueError, match="another repository/ref"):
        CompiledIntegrationPolicy(artifact).evaluate(
            subject, facts.model_copy(update={"publisher_fence": bad_fence})
        )


def test_ambiguous_publication_replays_exact_fenced_request(artifact):
    subject = make_subject(artifact, SubjectPhase.PUBLISHING)
    fence = Fence(
        target=BranchKey(repository_id="repo", branch="main"), owner_id="publisher", token=7
    )
    facts = make_facts(
        subject,
        ci=(CIEvidence(head_sha=HEAD, state=CIState.GREEN),),
        publisher_fence=fence,
        unresolved_writes=(
            UnresolvedWrite(
                journal_id="push",
                kind="main_push",
                target_ref="main",
                expected_old_sha=BASE,
                desired_sha=HEAD,
                state="unknown_after_push",
            ),
        ),
    )
    policy = CompiledIntegrationPolicy(artifact)
    decision = policy.evaluate(subject, facts)
    assert decision.rule == "reconcile-publication"
    assert decision.request.expected_old_sha == BASE
    assert decision.request.new_sha == HEAD
    assert decision.request.fence == fence
    still_unknown = outcome(decision, "unknown_after_push")
    schedule = policy.schedule(subject, decision, still_unknown, now=200)
    assert schedule.next_due_at == 230
    assert schedule.wait_reason == "ambiguous-publication"


def test_no_progress_stays_a_gate_and_continuation_uses_next_ordinal(artifact):
    subject = make_subject(
        artifact,
        SubjectPhase.REPAIRING,
        budget=WriterBudget(
            ordinal=3, intelligence_class="standard-high", started_at=100, deadline_at=500
        ),
    )
    policy = CompiledIntegrationPolicy(artifact)
    continued = policy.evaluate(subject, make_facts(subject, ladder_exhausted=True))
    assert continued.request.ordinal == 4
    held = policy.evaluate(subject, make_facts(subject, ladder_exhausted=True, no_progress=True))
    assert held.rule == "no-progress-gate"
    assert held.primitive == Primitive.GATE


def test_two_project_policies_change_exhaustion_without_python(artifact):
    template = load_definition_json((TEMPLATE / "artifact.json").read_text())
    continuous_subject = make_subject(artifact, SubjectPhase.REPAIRING)
    generic_subject = make_subject(template, SubjectPhase.REPAIRING)
    continued = CompiledIntegrationPolicy(artifact).evaluate(
        continuous_subject, make_facts(continuous_subject, ladder_exhausted=True)
    )
    gated = CompiledIntegrationPolicy(template).evaluate(
        generic_subject, make_facts(generic_subject, ladder_exhausted=True)
    )
    assert continued.primitive == Primitive.WRITER_FILE
    assert isinstance(gated.request, GateArgs)
    assert gated.request.no_default
    policy = CompiledIntegrationPolicy(template)
    schedule = policy.schedule(
        generic_subject, gated, outcome(gated, "created", gate_id="human-decision"), now=200
    )
    assert schedule.gate_id == "human-decision"
    assert schedule.next_due_at is None
    with pytest.raises(ValueError, match="exact gate_id"):
        policy.schedule(generic_subject, gated, outcome(gated, "created"), now=200)


def test_explicit_gate_hold_answer_is_binding(artifact):
    template = load_definition_json((TEMPLATE / "artifact.json").read_text())
    subject = make_subject(template, SubjectPhase.REPAIRING)
    policy = CompiledIntegrationPolicy(template)
    decision = policy.evaluate(subject, make_facts(subject, ladder_exhausted=True))
    held = outcome(decision, "answered", choice="hold", gate_id="human-decision")
    assert policy.phase(subject, decision, held) is None
    schedule = policy.schedule(subject, decision, held, now=200)
    assert schedule.gate_id == "human-decision"
    assert schedule.next_due_at is None
    retry = outcome(decision, "answered", choice="retry", gate_id="human-decision")
    assert policy.phase(subject, decision, retry) == SubjectPhase.BUILDING
    schedule = policy.schedule(subject, decision, retry, now=200)
    assert schedule.gate_id == "human-decision"
    assert schedule.next_due_at == 200
    with pytest.raises(ValueError, match="undeclared choice"):
        policy.schedule(subject, decision, outcome(decision, "answered", choice="eject"), now=200)


def test_gate_can_have_reviewed_timeout_default(raw_policy):
    inputs = raw_policy["tables"]["root_batch"]["actions"]["exhaustion-gate"]["inputs"]
    inputs["no_default"]["value"] = False
    inputs["default_choice"] = {"type": "literal", "value": "retry"}
    inputs["default_after_seconds"] = {"type": "literal", "value": 7200}
    assert IntegrationPolicy.model_validate(raw_policy)


def test_default_gate_reuse_does_not_restart_its_clock(artifact):
    template = load_definition_json((TEMPLATE / "artifact.json").read_text())
    raw = json.loads(canonical_bytes(template))
    inputs = raw["integration_policy"]["tables"]["root_batch"]["actions"]["exhaustion-gate"][
        "inputs"
    ]
    inputs["no_default"]["value"] = False
    inputs["default_choice"] = {"type": "literal", "value": "retry"}
    inputs["default_after_seconds"] = {"type": "literal", "value": 120}
    timed = type(artifact).model_validate(raw)
    subject = make_subject(timed, SubjectPhase.REPAIRING)
    policy = CompiledIntegrationPolicy(timed)
    decision = policy.evaluate(subject, make_facts(subject, ladder_exhausted=True))
    reused = outcome(decision, "reused", gate_id="human-decision", timeout_at=250)
    assert policy.schedule(subject, decision, reused, now=240).next_due_at == 250
    assert policy.schedule(subject, decision, reused, now=300).next_due_at == 250
    with pytest.raises(ValueError, match="persisted finite timeout"):
        policy.schedule(
            subject, decision, outcome(decision, "reused", gate_id="human-decision"), now=300
        )


def test_activation_does_not_replace_existing_subject_pin(artifact):
    existing = make_subject(artifact)
    old_pin = existing.policy
    raw = json.loads(canonical_bytes(artifact))
    action = raw["integration_policy"]["tables"]["root_batch"]["actions"]["cadence"]
    action["inputs"]["seconds"]["value"] = 120
    action["outcomes"]["waiting"]["seconds"] = 120
    raw["version"] = artifact.version + 1
    activated = type(artifact).model_validate(raw)
    assert activated.artifact_sha256() != old_pin.artifact_sha256
    assert existing.policy == old_pin
    assert (
        CompiledIntegrationPolicy(artifact).evaluate(existing, make_facts(existing)).policy
        == old_pin
    )
    with pytest.raises(ValueError, match="immutable pin"):
        CompiledIntegrationPolicy(activated).evaluate(existing, make_facts(existing))
    new_subject = make_subject(activated)
    decision = CompiledIntegrationPolicy(activated).evaluate(
        new_subject, make_facts(new_subject, members=())
    )
    assert decision.request.seconds == 120


def test_wrong_observation_and_outcome_are_refused(artifact):
    policy = CompiledIntegrationPolicy(artifact)
    subject = make_subject(artifact)
    with pytest.raises(ValueError, match="identity/version/phase"):
        policy.evaluate(subject, make_facts(subject, subject_version=1))
    decision = policy.evaluate(subject, make_facts(subject))
    with pytest.raises(ValueError, match="decided primitive"):
        policy.schedule(
            subject,
            decision,
            PrimitiveOutcome(primitive=Primitive.WAIT, outcome="waiting"),
            now=200,
        )


def test_table_only_change_is_executable_and_visible_in_review(artifact):
    raw = json.loads(canonical_bytes(artifact))
    raw["integration_policy"]["max_wait_seconds"] = 3601
    changed = type(artifact).model_validate(raw)
    proposal_diff = diff_definitions(artifact, changed)
    assert proposal_diff.executable_change
    assert proposal_diff.integration_policy
    response = diff_artifacts(
        artifact,
        changed,
        base_ref=artifact_ref_for(artifact),
        target_ref=artifact_ref_for(changed),
        contracts=RegistryContractLookup(),
        profiles=NullProfileLookup(),
    )
    assert response["executable_change"]
    row = next(row for row in response["rules"] if row["rule_id"] == "integration-policy")
    assert row["field_changes"]
