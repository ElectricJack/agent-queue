"""Package 6 T-10/T-12 — deterministic V1/V2 shadow evidence.

The comparison is deliberately a pure projection: the engine and the V1 test
harness own execution, while this module makes their recorded decisions
auditable without granting a waiver to an authorization mismatch.
"""

from __future__ import annotations

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


@pytest.mark.asyncio
async def test_cutover_report_command_uses_only_collected_evidence() -> None:
    class _Handler(PlaybookMigrationCommandsMixin):
        async def _cutover_report_inputs(self):
            return {
                "contract_fingerprint": "sha256:" + "a" * 64,
                "artifacts": ({"playbook_id": "default-pipeline", "health": "ready", "v1_available": True},),
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
