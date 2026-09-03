"""Package 6 T-10/T-11/T-12 — executable V1/V2 shadow parity evidence.

Two halves.  The first is the pure projection: :func:`compare` classifies
recorded decisions and never grants a waiver to an authorization mismatch.  The
second *runs both engines* over the checked-in event corpus —
``tests/conftest.py``'s ``PipelineEngine`` against the frozen V1 graph, and the
real ``PlaybookEngine`` in ``ExecutionMode.SHADOW`` against the reviewed V2
artifact — and records the result at
``tests/fixtures/playbooks/v2/parity-report.json``.

Refresh the recorded report with::

    pytest tests/test_playbook_shadow_parity.py --parity-record

CI never refreshes it: ``test_parity_report_is_current`` fails when a fresh run
disagrees with the committed record.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.commands.playbook_migration_commands import PlaybookMigrationCommandsMixin
from src.playbooks.migration import (
    EXPECTED_DIFFERENCES,
    AuthzDecision,
    CommandInvocation,
    ParityFinding,
    ShadowObservation,
    build_cutover_report,
    compare,
)
from tests.playbook_shadow_parity_harness import (
    PARITY_REPORT,
    RATIONALE_COVERAGE,
    CorpusCase,
    ScriptedResult,
    build_parity_report,
    collect_observations,
    load_corpus,
    load_v2_artifact,
    record_parity_report,
    run_v1_arm,
    run_v2_arm,
    shadow_engine,
    structural_parity,
    v1_node_id,
)


def _observation(*, arm: str, **changes: object) -> ShadowObservation:
    values: dict[str, object] = {
        "arm": arm,
        "event_id": "evt-1",
        "event_type": "task.completed",
        "rules_selected": ("review",),
        "node_path": ("review/ensure", "review/done"),
        "commands": (CommandInvocation(0, "ensure_task", '{"dedup_key":"review:1"}'),),
        "routing_outputs": {"review_id": "task-1"},
        "terminal": "completed",
        "authorization": (AuthzDecision("ensure_task", "service", True, None),),
    }
    values.update(changes)
    return ShadowObservation(**values)  # type: ignore[arg-type]


def test_compare_has_no_finding_for_identical_shadow_observations() -> None:
    assert compare(_observation(arm="v1"), _observation(arm="v2")) == ()


def test_compare_canonicalizes_field_by_field_and_keeps_unknown_differences_visible() -> None:
    findings = compare(
        _observation(arm="v1"),
        _observation(arm="v2", commands=(CommandInvocation(0, "ensure_task", "{}"),)),
    )

    assert len(findings) == 1
    assert findings[0].field == "commands"
    assert findings[0].classification == "unexplained"
    assert findings[0].rationale_id is None


def test_terminal_vocabulary_is_the_narrow_expected_difference() -> None:
    findings = compare(
        _observation(arm="v1", terminal="failed"),
        _observation(arm="v2", terminal="timed_out"),
    )

    assert [(finding.field, finding.classification, finding.rationale_id) for finding in findings] == [
        ("terminal", "expected_v2_semantics", "terminal-vocabulary")
    ]
    assert "terminal-vocabulary" in EXPECTED_DIFFERENCES


def test_authorization_difference_can_never_be_waived() -> None:
    findings = compare(
        _observation(arm="v1"),
        _observation(
            arm="v2",
            authorization=(AuthzDecision("ensure_task", "service", False, "denied"),),
        ),
    )
    assert findings[0].classification == "unexplained"
    with pytest.raises(ValueError, match="authorization"):
        ParityFinding(
            field="authorization",
            v1=True,
            v2=False,
            classification="expected_v2_semantics",
            rationale_id="terminal-vocabulary",
        )


def test_compare_rejects_wrong_arms_and_event_pairs() -> None:
    with pytest.raises(ValueError, match="v1"):
        compare(_observation(arm="v2"), _observation(arm="v2"))
    with pytest.raises(ValueError, match="event"):
        compare(_observation(arm="v1"), _observation(arm="v2", event_id="evt-2"))


def test_cutover_report_makes_every_gate_and_operational_backlog_visible() -> None:
    report = build_cutover_report(
        contract_fingerprint="sha256:" + "a" * 64,
        artifacts=(
            {
                "playbook_id": "default-pipeline",
                "scope": "system",
                "artifact_sha256": "sha256:" + "b" * 64,
                "source_sha256": "sha256:" + "c" * 64,
                "activation_health": "ready",
                "reviewed_by": "operator",
                "reviewed_at": "2026-09-01",
                "v1_available": True,
            },
        ),
        unresolved=(),
        acknowledged_disabled=(),
        pending_events=(
            {"playbook_id": "default-pipeline", "received_at": 90.0},
            {"playbook_id": "default-pipeline", "received_at": 95.0},
        ),
        active_v1_runs=(
            {"run_id": "v1-running", "status": "running", "started_at": 80.0},
            {"run_id": "v1-paused", "status": "paused", "started_at": 85.0},
        ),
        parity={"observations": 4, "identical": 3, "expected": 1, "unexplained": 0},
        now=100.0,
    )

    assert report["pending_events"] == {
        "total": 2,
        "oldest_age_seconds": 10.0,
        "by_playbook": {"default-pipeline": 2},
        "unavailable": False,
    }
    assert report["active_v1_runs"]["running"] == 1
    assert report["active_v1_runs"]["paused"] == 1
    assert report["active_v1_runs"]["oldest_age_seconds"] == 20.0
    assert report["rollback_ready"] is True
    assert report["cutover_eligible"] is False
    assert any("pending" in reason for reason in report["blocking_reasons"])
    assert any("V1" in reason for reason in report["blocking_reasons"])


def test_cutover_report_blocks_unresolved_parity_and_missing_rollback_artifact() -> None:
    report = build_cutover_report(
        contract_fingerprint="sha256:" + "a" * 64,
        artifacts=({"playbook_id": "default-pipeline", "activation_health": "ready"},),
        unresolved=({"playbook_id": "legacy", "disposition": "invalid", "reasons": []},),
        acknowledged_disabled=(),
        pending_events=(),
        active_v1_runs=(),
        parity={"observations": 1, "identical": 0, "expected": 0, "unexplained": 1},
        now=100.0,
    )

    assert report["rollback_ready"] is False
    assert report["cutover_eligible"] is False
    assert any("unresolved" in reason for reason in report["blocking_reasons"])
    assert any("unexplained" in reason for reason in report["blocking_reasons"])
    assert any("rollback" in reason for reason in report["blocking_reasons"])


def test_cutover_report_blocks_on_incomplete_artifact_evidence() -> None:
    """Null hashes are missing evidence, never evidence that nothing is wrong.

    An activation row names an artifact hash and nothing else, so a report
    assembled without joining the artifact renders four nulls per row.  Reading
    those as "fine" is how a fleet gets signed off on no evidence at all.
    """
    report = build_cutover_report(
        contract_fingerprint="sha256:" + "a" * 64,
        artifacts=(
            {
                "playbook_id": "default-pipeline",
                "activation_health": "ready",
                "v1_available": True,
            },
        ),
        unresolved=(),
        acknowledged_disabled=(),
        pending_events=(),
        active_v1_runs=(),
        parity={"observations": 1, "identical": 1, "expected": 0, "unexplained": 0,
                "recorded": True},
        now=100.0,
    )

    assert report["cutover_eligible"] is False
    assert report["rollback_ready"] is False
    assert any("incomplete artifact evidence" in r for r in report["blocking_reasons"])
    assert any("no recorded review" in r for r in report["blocking_reasons"])
    assert all("default-pipeline" in r for r in report["blocking_reasons"] if "(" in r)


def test_cutover_report_blocks_an_unreviewed_artifact_that_has_both_hashes() -> None:
    """Hashes prove which bytes are live; only a review says a human read them."""
    report = build_cutover_report(
        contract_fingerprint="sha256:" + "a" * 64,
        artifacts=(
            {
                "playbook_id": "default-pipeline",
                "artifact_sha256": "sha256:" + "b" * 64,
                "source_sha256": "sha256:" + "c" * 64,
                "activation_health": "ready",
                "v1_available": True,
            },
        ),
        unresolved=(),
        acknowledged_disabled=(),
        pending_events=(),
        active_v1_runs=(),
        parity={"observations": 1, "identical": 1, "expected": 0, "unexplained": 0,
                "recorded": True},
        now=100.0,
    )

    assert report["rollback_ready"] is False
    assert not any("incomplete artifact evidence" in r for r in report["blocking_reasons"])
    assert any("no recorded review" in r for r in report["blocking_reasons"])


@pytest.mark.asyncio
async def test_cutover_report_command_uses_only_collected_evidence() -> None:
    class _Handler(PlaybookMigrationCommandsMixin):
        async def _cutover_report_inputs(self):
            return {
                "contract_fingerprint": "sha256:" + "a" * 64,
                "artifacts": (
                    {
                        "playbook_id": "default-pipeline",
                        "artifact_sha256": "sha256:" + "b" * 64,
                        "source_sha256": "sha256:" + "c" * 64,
                        "health": "ready",
                        "reviewed_by": "operator",
                        "reviewed_at": "2026-09-01",
                        "v1_available": True,
                    },
                ),
                "unresolved": (),
                "acknowledged_disabled": (),
                "pending_events": (),
                "active_v1_runs": (),
                "parity": {"observations": 1, "identical": 1, "expected": 0, "unexplained": 0},
            }

    report = await _Handler()._cmd_playbook_cutover_report({})

    assert report["success"] is True
    assert report["cutover_eligible"] is True
    assert report["blocking_reasons"] == []


# ---------------------------------------------------------------------------
# The report must fail *closed* when it cannot read its own evidence
# ---------------------------------------------------------------------------


class _CleanEvidence:
    """A repository where every cutover evidence read reports a clean fleet.

    Construct it with ``method_name=SomeError(...)`` to make exactly that read
    raise while everything else stays clean — the shape that used to be
    indistinguishable from "nothing to report".
    """

    def __init__(self, **raises: BaseException) -> None:
        self._raises = raises

    def _maybe_raise(self, name: str) -> None:
        exc = self._raises.get(name)
        if exc is not None:
            raise exc

    async def list_playbook_activations(self) -> list[dict]:
        self._maybe_raise("list_playbook_activations")
        fixture_root = PARITY_REPORT.parent / "default-pipeline"
        artifact = json.loads((fixture_root / "artifact.json").read_text(encoding="utf-8"))
        return [
            {
                "playbook_id": "default-pipeline",
                "scope": "system",
                "enabled": True,
                "artifact_sha256": (fixture_root / "artifact.sha256")
                .read_text(encoding="utf-8")
                .strip(),
                "source_digest": artifact["source_hash"],
                "health": "ready",
            }
        ]

    async def list_pending_events(self, limit: int | None = None) -> list[dict]:
        self._maybe_raise("list_pending_events")
        return []

    async def list_playbook_runs(
        self, status: str | None = None, limit: int | None = None
    ) -> list[dict]:
        self._maybe_raise("list_playbook_runs")
        return []

    async def list_playbook_migration_acks(self) -> list[dict]:
        self._maybe_raise("list_playbook_migration_acks")
        return []


class _EvidenceHandler(PlaybookMigrationCommandsMixin):
    """The real ``_cutover_report_inputs`` over a controllable repository."""

    def __init__(self, db: _CleanEvidence, *, store_raises: BaseException | None = None) -> None:
        self.db = db
        self._store_raises = store_raises

    async def _migration_inventory(self):
        return SimpleNamespace(entries=(), blocking=lambda: ())

    async def _cutover_live_health(self, _activation_rows):
        return {("default-pipeline", "system", ""): "ready"}

    async def _cutover_release_evidence(self, _activation_rows):
        return {
            "success": True,
            "stale": [],
            "unverified": [],
            "evidence_errors": [],
            "blocking_reasons": [],
        }

    def _migration_store(self):
        def _list_all():
            if self._store_raises is not None:
                raise self._store_raises
            return [("system", None, SimpleNamespace(id="default-pipeline"))]

        return SimpleNamespace(list_all=_list_all)


@pytest.fixture
def _at_repo_root(monkeypatch):
    """``REVIEWED_FIXTURE_ROOT`` is repo-relative; pin cwd so parity is read."""
    monkeypatch.chdir(PARITY_REPORT.parents[4])


@pytest.mark.asyncio
async def test_cutover_report_is_eligible_when_every_evidence_read_succeeds(
    _at_repo_root,
) -> None:
    """The control: a genuinely clean fleet still certifies."""
    report = await _EvidenceHandler(_CleanEvidence())._cmd_playbook_cutover_report({})

    assert report["evidence_errors"] == []
    assert report["blocking_reasons"] == []
    assert report["cutover_eligible"] is True
    assert report["pending_events"]["unavailable"] is False
    assert report["active_v1_runs"]["unavailable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "source"),
    [
        ("list_pending_events", "pending_events"),
        ("list_playbook_runs", "active_v1_runs"),
        ("list_playbook_activations", "activations"),
        ("list_playbook_migration_acks", "acknowledgements"),
    ],
)
async def test_cutover_report_blocks_when_an_evidence_read_raises(
    _at_repo_root, method: str, source: str
) -> None:
    """One failed query, everything else clean, and cutover is refused.

    The regression: these exceptions used to become empty lists, which the
    report then read as "no pending events" and "no active V1 runs" — the
    exact evidence it exists to weigh.
    """
    handler = _EvidenceHandler(_CleanEvidence(**{method: RuntimeError("database is locked")}))

    report = await handler._cmd_playbook_cutover_report({})

    assert report["cutover_eligible"] is False
    assert source in {row["source"] for row in report["evidence_errors"]}
    assert all("database is locked" in row["error"] for row in report["evidence_errors"])
    assert any(
        f"{source!r} could not be read" in reason and "database is locked" in reason
        for reason in report["blocking_reasons"]
    )


@pytest.mark.asyncio
async def test_cutover_report_marks_the_section_an_unreadable_source_fed(
    _at_repo_root,
) -> None:
    """A section built from an unread source says so, not just the summary."""
    pending = await _EvidenceHandler(
        _CleanEvidence(list_pending_events=RuntimeError("boom"))
    )._cmd_playbook_cutover_report({})
    assert pending["pending_events"] == {
        "total": 0,
        "oldest_age_seconds": None,
        "by_playbook": {},
        "unavailable": True,
    }
    assert pending["active_v1_runs"]["unavailable"] is False

    runs = await _EvidenceHandler(
        _CleanEvidence(list_playbook_runs=RuntimeError("boom"))
    )._cmd_playbook_cutover_report({})
    assert runs["active_v1_runs"]["unavailable"] is True
    assert runs["pending_events"]["unavailable"] is False
    # One entry per status queried: the report names each read it lost.
    assert [row["source"] for row in runs["evidence_errors"]] == [
        "active_v1_runs",
        "active_v1_runs",
    ]


@pytest.mark.asyncio
async def test_cutover_report_withholds_rollback_readiness_it_cannot_evidence(
    _at_repo_root,
) -> None:
    """No V1 store read means no claim that a rollback target exists."""
    handler = _EvidenceHandler(_CleanEvidence(), store_raises=RuntimeError("vault gone"))

    report = await handler._cmd_playbook_cutover_report({})

    assert report["rollback_ready"] is False
    assert report["cutover_eligible"] is False
    assert "v1_store" in {row["source"] for row in report["evidence_errors"]}


def test_cutover_report_evidence_errors_default_to_none_recorded() -> None:
    """Callers that pass no ``evidence_errors`` are unaffected."""
    report = build_cutover_report(
        contract_fingerprint="sha256:" + "a" * 64,
        artifacts=(
            {
                "playbook_id": "p",
                "artifact_sha256": "sha256:" + "b" * 64,
                "source_sha256": "sha256:" + "c" * 64,
                "activation_health": "ready",
                "reviewed_by": "operator",
                "reviewed_at": "2026-09-03",
                "v1_available": True,
            },
        ),
        unresolved=(),
        acknowledged_disabled=(),
        pending_events=(),
        active_v1_runs=(),
        parity={"observations": 1, "identical": 1, "expected": 0, "unexplained": 0},
        now=100.0,
    )

    assert report["evidence_errors"] == []
    assert report["cutover_eligible"] is True


# ===========================================================================
# T-10 / T-11 / T-12 — the executable arms
# ===========================================================================


@pytest.fixture(scope="module", autouse=True)
def _maybe_record(request) -> None:
    import asyncio

    if request.config.getoption("--parity-record"):
        asyncio.run(record_parity_report())


@pytest.fixture(scope="module")
def observations():
    """Both arms over the whole corpus, run once for the module."""
    import asyncio

    return asyncio.run(collect_observations())


@pytest.fixture(scope="module")
def artifact():
    return load_v2_artifact()


def _case(name: str) -> CorpusCase:
    return next(case for case in load_corpus() if case.name == name)


@pytest.mark.asyncio
async def test_neither_arm_executes_commands() -> None:
    """§4.4's negative assertion, and the first one that has to hold.

    A handler double that raises on every call is wired into both arms.  Two
    complete observations still come back: the V1 arm's handler is a recorder
    that reaches nothing real, and the V2 arm is a shadow dispatch whose
    executors have no code path to ``registration.invoke``.
    """
    from src.commands.principal import TRUSTED_LOCAL
    from src.playbooks.executors.base import ExecutionMode
    from tests.playbook_shadow_parity_harness import RaisingHandler

    art = load_v2_artifact()
    case = _case("task-completed-with-branch-and-pr")
    engine = shadow_engine(art, handler=RaisingHandler())

    dispatch = await engine.dispatch_event(
        dict(case.event), TRUSTED_LOCAL, mode=ExecutionMode.SHADOW
    )

    assert dispatch.rules_selected == ("per-task-review", "per-branch-final-review")
    v1 = await run_v1_arm(case, art)
    v2, _dispatch, _projection = await run_v2_arm(case, art)
    assert v1.commands and v2.commands


def test_corpus_covers_every_guard_outcome(artifact) -> None:
    """T-11's coverage rule, stated over the guards the artifact actually has.

    Every rule needs an event that selects it.  Every *guarded* rule also
    needs one where the guard rejects it — that is the pair the rewrite could
    have broken.  The three ungated rules have nothing to reject with, so
    demanding a failing event for them would only invite a meaningless
    fixture; what is asserted for them instead is that an event of their
    trigger type exists and selects them.
    """
    import asyncio

    passing: dict[str, list[str]] = {rule.id: [] for rule in artifact.rules}
    failing: dict[str, list[str]] = {rule.id: [] for rule in artifact.rules}
    for case in load_corpus():
        selected = asyncio.run(run_v1_arm(case, artifact)).rules_selected
        for rule in artifact.rules:
            if rule.trigger.event_type != case.event_type:
                continue
            (passing if rule.id in selected else failing)[rule.id].append(case.name)

    assert all(passing.values()), f"rules with no selecting event: {passing}"
    guarded = {rule.id for rule in artifact.rules if rule.guard is not None}
    assert guarded, "the artifact lost its guards"
    assert all(failing[rule_id] for rule_id in guarded), (
        f"guarded rules with no rejecting event: "
        f"{ {rule_id: failing[rule_id] for rule_id in guarded} }"
    )


def test_identical_rule_selection(observations) -> None:
    """The single most important assertion: the rewrite fires the same rules."""
    mismatched = [
        (v1.event_id, v1.rules_selected, v2.rules_selected)
        for v1, v2, _ in observations
        if v1.rules_selected != v2.rules_selected
    ]
    assert mismatched == []


def test_identical_command_sequence(observations) -> None:
    """Every command, in order, with canonicalised arguments (§3.5.1)."""
    for v1, v2, findings in observations:
        assert [c.command for c in v1.commands] == [
            c.command for c in v2.commands
        ], v1.event_id
        for finding in findings:
            if finding.field == "commands":
                assert finding.classification == "expected_v2_semantics", v1.event_id


def test_identical_node_paths_and_terminals(observations) -> None:
    for v1, v2, _ in observations:
        assert v1.node_path == v2.node_path, v1.event_id
        assert v1.terminal == v2.terminal, v1.event_id


def test_expected_differences_are_registered(observations) -> None:
    for _v1, _v2, findings in observations:
        for finding in findings:
            if finding.classification == "expected_v2_semantics":
                assert finding.rationale_id in EXPECTED_DIFFERENCES


def test_no_unexplained_findings(observations) -> None:
    unexplained = [
        (v1.event_id, finding.field, finding.v1, finding.v2)
        for v1, _v2, findings in observations
        for finding in findings
        if finding.classification == "unexplained"
    ]
    assert unexplained == []


def test_authorization_never_differs(observations) -> None:
    """The exit gate's authorization half, over the reviewed capability set."""
    for v1, v2, _ in observations:
        assert v1.authorization == v2.authorization, v1.event_id
        assert all(decision.allowed for decision in v2.authorization), v1.event_id


def test_no_unused_rationales(observations) -> None:
    """A rationale nothing exercises is a stale waiver (T-10 assertion 5)."""
    assert set(RATIONALE_COVERAGE) == set(EXPECTED_DIFFERENCES)
    exercised = {
        finding.rationale_id
        for _v1, _v2, findings in observations
        for finding in findings
        if finding.rationale_id
    }
    assert "null-template-part-rendered" in exercised


@pytest.mark.asyncio
async def test_projection_agrees_with_the_engine_at_the_shadow_frontier() -> None:
    """Pins the projection to the engine wherever shadow resolves anything.

    ``ShadowCommandExecutor`` records a step's arguments and then returns
    ``UNRESOLVED``, so shadow observes each rule's first event-derived command
    and no further.  Every argument set it *does* record must agree, key for
    key, with what the harness projects for the same step — otherwise the
    projection has drifted from the engine it stands in for.  The engine's
    record is the validated argument *model*, so it also carries the contract's
    unset optional fields; only the authored keys are compared.
    """
    from src.playbooks.executors.base import ExecutionMode
    from tests.playbook_shadow_parity_harness import parity_principal

    art = load_v2_artifact()
    compared = 0
    for case in load_corpus():
        dispatch = await shadow_engine(art).dispatch_event(
            dict(case.event), parity_principal(), mode=ExecutionMode.SHADOW
        )
        _v2, _d, projection = await run_v2_arm(case, art)
        projected = {step_id: args for step_id, _command, args in projection.calls}
        for step_id, command, engine_args in dispatch.commands:
            assert step_id in projected, f"{case.name}: projection never reached {step_id}"
            assert art.steps[step_id].command == command
            engine = json.loads(engine_args)
            for key, value in projected[step_id].items():
                assert engine[key] == value, f"{case.name}:{step_id}:{key}"
            compared += 1
    assert compared, "shadow recorded no commands at all — the frontier check is vacuous"


@pytest.mark.parametrize(
    "playbook_id", ["default-assignment-routing", "memory-consolidation", "coding-reflection"]
)
def test_llm_playbooks_get_structural_parity_only(playbook_id: str) -> None:
    """§4.5: compare the source plus every promised V2 structural field.

    Their V1 behaviour is an LLM call, so there is nothing deterministic to
    compare per run.  The limit is recorded in the report's
    ``coverage_limits``; asserting it here keeps the two in step.
    """
    v1, v2 = structural_parity(playbook_id)
    assert v1 == v2
    report = json.loads(PARITY_REPORT.read_text(encoding="utf-8"))
    assert playbook_id in report["structural_only_playbooks"]
    assert any("structurally" in limit for limit in report["coverage_limits"])


@pytest.mark.parametrize(
    "category",
    [
        "scope",
        "rules",
        "triggers",
        "steps",
        "profiles",
        "budgets",
        "capabilities",
        "output_schemas",
        "transitions",
    ],
)
def test_llm_structural_parity_detects_artifact_mutations(category: str) -> None:
    """A change in any promised category must alter the V2 projection."""
    playbook_id = "coding-reflection"
    fixture = PARITY_REPORT.parent / playbook_id / "artifact.json"
    artifact = json.loads(fixture.read_text(encoding="utf-8"))
    mutated = deepcopy(artifact)
    entry_step = mutated["rules"][0]["entry_step"]

    if category == "scope":
        mutated["scope"] = {"type": "project", "project_id": "mutated"}
    elif category == "rules":
        mutated["rules"][0]["entry_step"] = "mutated-entry"
    elif category == "triggers":
        mutated["rules"][0]["trigger"]["event_type"] = "task.mutated"
    elif category == "steps":
        mutated["steps"][entry_step]["rule"] = "mutated-rule"
    elif category == "profiles":
        mutated["steps"][entry_step]["profile_id"] = "mutated-profile"
    elif category == "budgets":
        mutated["steps"][entry_step]["budget"]["max_calls"] += 1
    elif category == "capabilities":
        mutated["steps"][entry_step]["tool_use"]["aq_commands"].append("mutated_command")
    elif category == "output_schemas":
        mutated["steps"][entry_step]["output_schema"]["required"].append("mutated_field")
    elif category == "transitions":
        mutated["steps"][entry_step]["transitions"]["completed"] = "reflect-completed--failed"
    else:  # pragma: no cover - the parameter list is the closed category set
        raise AssertionError(category)

    expected, actual = structural_parity(playbook_id, artifact=mutated)
    assert expected["artifact"][category] != actual["artifact"][category]


# --- one executable demonstration per registered rationale -----------------


@pytest.mark.asyncio
async def test_run_per_rule_is_exercised() -> None:
    """V1 covered both rules with one run row; V2 starts one run per rule."""
    from src.playbooks.executors.base import ExecutionMode
    from tests.playbook_shadow_parity_harness import parity_principal

    art = load_v2_artifact()
    case = _case("task-completed-with-branch-and-pr")
    dispatch = await shadow_engine(art).dispatch_event(
        dict(case.event), parity_principal(), mode=ExecutionMode.SHADOW
    )
    assert len(dispatch.rules_selected) == 2
    assert len(set(dispatch.run_ids)) == 2
    # Run identity is deliberately not part of the compared surface.
    assert not hasattr(ShadowObservation, "run_id")


@pytest.mark.asyncio
async def test_rule_failure_isolation_is_exercised() -> None:
    """A raising rule ends V1's dispatch and leaves V2's sibling running."""
    from src.playbooks.executors.base import ExecutionMode
    from tests.playbook_shadow_parity_harness import parity_principal

    art = load_v2_artifact()
    base = _case("task-completed-with-branch-and-pr")
    exploding = CorpusCase(
        name=base.name,
        event=base.event,
        oracle={**base.oracle, "ensure_task": ScriptedResult("created", {}, raises="boom")},
    )

    v1 = await run_v1_arm(exploding, art)
    assert v1.terminal == "failed"
    assert v1.rules_selected == ("per-task-review",)  # the sibling never ran

    dispatch = await shadow_engine(art).dispatch_event(
        dict(base.event), parity_principal(), mode=ExecutionMode.SHADOW
    )
    assert dispatch.rules_selected == ("per-task-review", "per-branch-final-review")
    assert len(set(dispatch.run_ids)) == 2


def test_loop_frame_shape_is_exercised(artifact) -> None:
    """V2 splits a V1 loop node into a foreach frame plus a body step."""
    from src.playbooks.definition import ForEachStep

    loop = artifact.steps["per-task-review--gate-downstream"]
    assert isinstance(loop, ForEachStep)
    body = artifact.steps[loop.body_entry]
    assert body.type == "command"
    # Both fold onto the single V1 node id, which is why node_path compares.
    assert v1_node_id("per-task-review--gate-downstream", artifact) == v1_node_id(
        loop.body_entry, artifact
    )
    # …and the per-iteration commands are what is actually compared.
    import asyncio

    case = _case("task-completed-with-downstream")
    v1 = asyncio.run(run_v1_arm(case, artifact))
    v2, _dispatch, _projection = asyncio.run(run_v2_arm(case, artifact))
    assert [c.command for c in v1.commands].count("gate_create") == 2
    assert [c.command for c in v2.commands] == [c.command for c in v1.commands]


def test_unassigned_ref_rejected_is_exercised() -> None:
    """V1 blanks an unassigned reference; V2's resolver refuses it."""
    from src.playbooks.expressions import (
        BindingRef,
        ResolutionScope,
        ValueResolutionError,
        resolve_value,
    )
    from src.playbooks.pipeline_runner import _substitute

    assert _substitute("{{outputs.missing.task_id}}", {}, {}) is None
    assert _substitute("id={{outputs.missing.task_id}}", {}, {}) == "id="
    with pytest.raises(ValueResolutionError):
        resolve_value(
            BindingRef(type="binding_ref", binding="missing", path="task_id"),
            ResolutionScope(),
        )


# --- T-12: the committed report --------------------------------------------


def test_parity_report_is_current(observations, artifact) -> None:
    """A fresh run must reproduce the committed record, byte for byte.

    The report is evidence a reader can check on a machine that cannot run
    this suite, so it is refreshed deliberately (``--parity-record``) and never
    silently in CI.
    """
    from src.playbooks.definition import artifact_sha256

    fresh = build_parity_report(observations, artifact_sha256=artifact_sha256(artifact))
    committed = json.loads(PARITY_REPORT.read_text(encoding="utf-8"))
    assert fresh == committed, "run pytest tests/test_playbook_shadow_parity.py --parity-record"


def test_recorded_report_is_clean() -> None:
    report = json.loads(PARITY_REPORT.read_text(encoding="utf-8"))
    assert report["unexplained"] == 0
    assert report["observations"] == len(load_corpus())
    assert report["identical"] + report["expected"] == report["observations"]


def test_recorded_report_unblocks_the_cutover_report_parity_gate() -> None:
    """The committed record is what `playbook_cutover_report` reads (§3.7).

    Before this package the file was absent and the command fell back to
    ``observations: 0``, which blocks cutover.  Asserting the real record
    satisfies the gate keeps the two halves of the evidence in one place.
    """
    report = json.loads(PARITY_REPORT.read_text(encoding="utf-8"))
    built = build_cutover_report(
        contract_fingerprint="sha256:" + "a" * 64,
        artifacts=(
            {
                "playbook_id": "default-pipeline",
                "activation_health": "ready",
                "artifact_sha256": report["artifact_sha256"],
                "reviewed_by": "fixture",
                "reviewed_at": "2026-09-03",
                "v1_available": True,
            },
        ),
        unresolved=(),
        acknowledged_disabled=(),
        pending_events=(),
        active_v1_runs=(),
        parity=report,
        now=100.0,
    )
    assert not any("parity" in reason for reason in built["blocking_reasons"])
    assert not any("unexplained" in reason for reason in built["blocking_reasons"])
