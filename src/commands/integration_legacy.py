"""The integration operator surface and its gated legacy exceptions.

Integration-train simplification §5.1 consolidates 49 operator controls to the
decisions a person makes and the reads that explain them.  Every control that
did not survive is listed here with its Appendix B class, what replaces it,
and the condition that must hold before it is deleted.  Until then it stays
callable under ``aq integration legacy`` (its old flat path still resolves).

``tests/test_integration_surface.py`` holds the surface to this table: every
``aq integration`` command and every operator integration control id is
either approved or listed here, and the supervisor's grants agree with the
``supervisor`` column.  This module is a leaf: it imports nothing from the
CLI, the API or the handlers.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

Kind = Literal["hand", "decision", "migration", "diagnostic"]


class OperatorControl(NamedTuple):
    """One approved control of the consolidated surface."""

    path: str
    command_id: str
    kind: Kind


class LegacyControl(NamedTuple):
    """One pre-consolidation control kept until ``removal_gate`` holds."""

    path: str
    command_id: str | None
    kind: Kind
    replacement: str
    removal_gate: str
    #: Whether a live supervisor of the project may run it (handler and grant).
    supervisor: bool = True


#: §5.1: four human decisions, two diagnostics, and ``flush``.  The spec's
#: table counts six controls (4 DECISION + 2 DIAGNOSTIC) yet keeps ``flush`` in
#: the HAND row as "set ``next_due_at = now``", so the surface has seven.
APPROVED_INTEGRATION_CONTROLS: tuple[OperatorControl, ...] = (
    OperatorControl("integration gate answer", "integration_gate_answer", "decision"),
    OperatorControl("integration authorize", "integration_authorize_root", "decision"),
    OperatorControl("integration policy activate", "integration_policy_activate", "decision"),
    OperatorControl("integration hold", "integration_hold", "decision"),
    OperatorControl("integration status", "integration_status", "diagnostic"),
    OperatorControl("integration explain", "integration_explain", "diagnostic"),
    OperatorControl("integration flush", "integration_flush", "hand"),
)

#: A repair session's own protocol step (Appendix B row 3), not an operator
#: control; workers call it and the surface count excludes it.
AGENT_PROTOCOL_CONTROLS: tuple[OperatorControl, ...] = (
    OperatorControl(
        "integration resolve-candidate-member", "integration_resolve_candidate_member", "hand"
    ),
)

#: Steps the integration playbooks and services call, not operator controls;
#: Appendix B does not count them.  A live supervisor may re-drive the six
#: root-train steps it is granted (``_SUPERVISOR_REDRIVE_CAPABILITIES``), but
#: no person is asked to run any of them.
PLAYBOOK_INTEGRATION_COMMANDS: frozenset[str] = frozenset({
    "integration_build_candidate",
    "integration_checkpoint_parent",
    "integration_ci_evidence",
    "integration_cleanup",
    "integration_complete_parent",
    "integration_delivery_readiness",
    "integration_file_children",
    "integration_mutate_hierarchy",
    "integration_parent_verify",
    "integration_promote_main",
    "integration_push_conflict_resolution",
    "integration_reconcile_promotion",
    "integration_record_repair",
    "integration_release",
    "integration_repair_close_current",
    "integration_repair_dispatch",
    "integration_repair_start",
    "integration_repair_timeout",
    "integration_resolve_conflict",
    "integration_schedule_due",
    "integration_seal",
})

_GATE = "aq integration gate answer"
_POLICY = "aq integration policy activate"
_RECONCILER = (
    "reconciler subjects (§4 blocker catalogue): the observer fact and the policy line act "
    "on the next visit"
)
_TRUST = "aq doctor --check integration.trust"
#: Mechanical and decision controls go with the module that implements them.
_ENGINE_GATE = (
    "every repository's subjects run on the reconciler engine (engine-transfer applied, "
    "doctor integration.subjects_overdue reads zero) and the module behind it is "
    "deleted (§5.2)"
)


def _hand(path: str, command_id: str | None, line: str, **kw) -> LegacyControl:
    return LegacyControl(path, command_id, "hand", f"{_RECONCILER}; {line}", _ENGINE_GATE, **kw)


def _decision(path: str, command_id: str | None, choice: str, **kw) -> LegacyControl:
    return LegacyControl(path, command_id, "decision", f"{_GATE} ({choice})", _ENGINE_GATE, **kw)


def _migration(path: str, command_id: str | None, gate: str, **kw) -> LegacyControl:
    return LegacyControl(path, command_id, "migration", "none: one-off upgrade path", gate, **kw)


LEGACY_INTEGRATION_CONTROLS: tuple[LegacyControl, ...] = (
    # Appendix B.1 — ``aq integration`` subcommands.
    _hand("integration legacy record-noop", "integration_record_noop",
          "the collector records a no-code child's receipt"),
    _decision("integration legacy eject", "integration_eject", "the `eject` choice"),
    LegacyControl("integration legacy enable", "integration_enable", "decision",
                  f"{_POLICY} --mode <mode>", _ENGINE_GATE),
    _migration("integration legacy reconcile-unmaterialized",
               "integration_reconcile_unmaterialized",
               "no project's preflight reports a task with repo_id IS NULL"),
    _migration("integration legacy waive-history", "integration_waive_history",
               "no project's enable preflight reports a legacy_pr_merge_gate blocker"),
    _decision("integration legacy resume", "integration_resume", "the `retry` choice"),
    _decision("integration legacy abort", "integration_abort", "the `eject` choice for every member"),
    _hand("integration legacy retry-cleanup", "integration_retry_cleanup",
          "the cleanup primitive retries failed items"),
    _hand("integration legacy release-owner", "integration_release_owner",
          "the writer stop-proof releases a stranded owner"),
    _hand("integration legacy reserve-owner", "integration_reserve_owner",
          "the writer reserves a missing owner"),
    _hand("integration legacy release-stale-owners", "integration_release_stale_owners",
          "drain releases finished owners"),
    _migration("integration legacy adopt-legacy-deliveries",
               "integration_adopt_legacy_deliveries",
               "no status shows missing_receipt/no_parent_collection and adopt-legacy-deliveries "
               "answers nothing_to_adopt for every project"),
    _migration("integration legacy bind-legacy-repositories",
               "integration_bind_legacy_repositories",
               "no observe/hierarchy/train project has a terminal hierarchy task with "
               "repo_id IS NULL"),
    _hand("integration legacy close-delivered-pr", "integration_close_delivered_pr",
          "delivery closes a PR whose content landed"),
    _hand("integration legacy clear-stale-request", "integration_clear_stale_request",
          "subjects carry due times instead of sweep requests (aq integration flush)"),
    _hand("integration legacy redrive-root", "integration_redrive_root",
          "the root subject opens its PR or collects"),
    _migration("integration legacy materialize-root", "integration_materialize_root",
               "doctor reports no unmaterialized_train_pr"),
    LegacyControl("integration legacy authorize-root", "integration_authorize_root", "decision",
                  "aq integration authorize (the same command, renamed)",
                  "no installed prompt or profile names `authorize-root`"),
    _hand("integration legacy redrive-child", "integration_redrive_child",
          "the parent subject collects a delivered child"),
    _hand("integration legacy reopen-collection", "integration_reopen_collection",
          "a cancelled collection is a fact the parent's policy line answers"),
    _migration("integration legacy rebind-reused-identity", "integration_rebind_reused_identity",
               "the legacy integration.reused_task_identity query finds zero rows"),
    _hand("integration legacy rebind-repair", "integration_rebind_repair",
          "a repair stage follows the current intent"),
    _hand("integration legacy recover-preserved-repair", "integration_recover_preserved_repair",
          "preserved work is a writer the next stage resumes"),
    _hand("integration legacy rebind-detached-repair", "integration_rebind_detached_repair",
          "a detached delegate's head is a fact the stage rebinds to"),
    _hand("integration legacy release-delegates", "integration_release_delegates",
          "terminal delegates are retired every tick"),
    _hand("integration legacy recover-candidate-member", "integration_recover_candidate_member",
          "a pushed candidate member is accepted or rejected on the next visit"),
    LegacyControl("integration legacy develop", "integration_develop", "decision",
                  f"{_POLICY} --mode development --policy <file>", _ENGINE_GATE),
    _decision("integration legacy adopt", "integration_adopt", "the completion/equivalence choice"),
    _hand("integration legacy sweep", "integration_development_sweep",
          "aq integration flush makes the development subjects due"),
    _decision("integration legacy settle-parked", "integration_settle_parked",
              "the `not owed`/`dismiss` choice"),
    _migration("integration legacy migrate-provenance", "integration_migrate_provenance",
               "every development project's provenance inventory reports zero_fallback=true"),
    _decision("integration legacy cancel-preserving", "integration_cancel_preserving",
              "the `cancel` choice"),
    LegacyControl("integration legacy onboard-train", None, "diagnostic",
                  "aq integration status", _ENGINE_GATE),
    LegacyControl("integration legacy trust-manifest", "integration_trust_manifest",
                  "diagnostic", _TRUST, "integration.trust renders the manifest's items"),
    LegacyControl("integration legacy app-verify", "integration_app_verify", "diagnostic",
                  _TRUST, "integration.trust renders the manifest's items"),
    LegacyControl("integration legacy app-setup", None, "diagnostic", _TRUST,
                  "integration.trust renders the manifest's items and its --apply step"),
    # Added after the Appendix B survey.
    _hand("integration legacy recover-parent-head", "integration_recover_parent_head",
          "a completed parent repair's head is a fact the parent subject adopts"),
    _hand("integration legacy settle-delivered-batch", "integration_settle_delivered_batch",
          "a batch whose candidate landed settles on the next visit"),
    # The cutover itself: a person runs it from shadow-report evidence.
    _migration("integration legacy engine-transfer", "integration_engine_transfer",
               "every repository's subjects run on the reconciler engine", supervisor=False),
    LegacyControl("integration legacy shadow-report", "integration_shadow_report", "diagnostic",
                  "aq integration explain <subject>",
                  "every repository's subjects run on the reconciler engine"),
    # Appendix B.2 — related commands outside ``aq integration``.  They stay
    # in their own groups; deleting them is the module work of §5.2.
    _hand("system integration-recover-unwritten-resolution",
          "integration_recover_unwritten_resolution",
          "an unwritten resolution reservation is superseded on the next visit",
          supervisor=False),
    _hand("system integration-transfer-owner", "integration_transfer_owner",
          "the writer transfers ownership under its fence"),
    _hand("task deliver", "task_deliver", "the root subject delivers a passed task"),
    # ``aq task close --obsolete`` is a flag on the task close command.
    _decision("task close", None, "the `obsolete` choice"),
    # The ci-main-sentinel playbook's read and write; no supervisor grant.
    LegacyControl("git ci-baseline-status", "ci_baseline_status", "diagnostic",
                  "aq integration status", _ENGINE_GATE, supervisor=False),
    _decision("git ci-repair-adopt", "ci_repair_adopt", "the `adopt` choice", supervisor=False),
)


class LegacyDoctorCheck(NamedTuple):
    """One pre-consolidation ``integration.*`` doctor check kept until its gate holds."""

    check_id: str
    replacement: str
    removal_gate: str


#: §5.5: twenty checks become three (``src/doctor/integration_subject_checks.py``).
APPROVED_INTEGRATION_DOCTOR_CHECKS: tuple[str, ...] = (
    "integration.subjects_overdue",
    "integration.subjects_held",
    "integration.trust",
)

_STATUS = "aq integration status: an observer fact on the subject"
_GONE = "none: a state the reconciler cannot reach"


def _check(check_id: str, replacement: str) -> LegacyDoctorCheck:
    return LegacyDoctorCheck(f"integration.{check_id}", replacement, _ENGINE_GATE)


#: The checks that read pre-reconciler state.  They stay registered (and run)
#: until the module whose state they read is deleted.
LEGACY_INTEGRATION_DOCTOR_CHECKS: tuple[LegacyDoctorCheck, ...] = (
    _check("reviewed_file_guard", _STATUS),
    _check("delivery_path", _STATUS),
    _check("operational", _STATUS),
    _check("orphaned_operations", _GONE),
    LegacyDoctorCheck("integration.reused_task_identity", _GONE,
                      "the legacy integration.reused_task_identity query finds zero rows"),
    _check("unreviewed_prs", _STATUS),
    _check("branch_discards", _GONE),
    _check("stranded_fences", _GONE),
    _check("missing_canonical_owners", _GONE),
    _check("stranded_dependents", _STATUS),
    _check("development_publisher_stalled", "integration.subjects_overdue"),
    _check("development_conflicts_unrepaired", "integration.subjects_held"),
    _check("stranded_delegates", _GONE),
    LegacyDoctorCheck("integration.app_mode", "integration.trust (the same probe, renamed)",
                      "no installed prompt or runbook names integration.app_mode"),
    _check("stale_repair_intents", _GONE),
    _check("missing_repair_owners", _GONE),
    _check("stale_schedule", "integration.subjects_overdue"),
    _check("stuck_children", "integration.subjects_overdue"),
    _check("blocked_collectors", _STATUS),
    _check("finished_branch_owners", _GONE),
)


def legacy_control(path: str) -> LegacyControl | None:
    """The legacy entry for CLI ``path`` (``"integration legacy enable"``)."""
    for control in LEGACY_INTEGRATION_CONTROLS:
        if control.path == path:
            return control
    return None
