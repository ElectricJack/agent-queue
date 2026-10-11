# Human asks implementation acceptance manifest

Implementation specification: `rev-sharp-zenith` r1; design: `rev-fair-ridge` r3.
This manifest records code evidence. It does not certify production enablement.
The [operator runbook](../guides/human-asks-rollout.md) defines live evidence,
missing-evidence handling, transfer and rollback. H08 integrates H01–H07 from
`526d811d6`; H08-OPS and H09-OPS own the production gates.

Test references below are exact pytest node names unless a browser check or Vitest
file is named. Parameterized tests cover all collected variants. Run the complete
area commands after this table; a node reference alone is not a passing receipt.

| Acceptance | Implementation and schema evidence | Exact verification | Live owner / limitation |
|---|---|---|---|
| A01 Context and ownership | `src/asks/models.py`, `service.py`; `human_asks.context_state` and owner/version fields in migration 91 | `tests/test_ask_sources.py::test_missing_context_refuses_without_creating_ask`; `::test_missing_ownership_refuses_publication`; `::test_legacy_context_needed_preserves_source_link_without_choices` | H08-OPS samples actual producer context and held work. |
| A02 One identity and replay | `src/asks/reconcile.py` exact review `await_id`/back-reference, explicit incident source, immutable proposal lookup; `ask_source_aliases` uniqueness and reconciliation cases (91) | `tests/test_ask_migration.py::test_reconcile_dry_run_cursor_and_exact_source_aliases`; `::test_alias_disagreement_quarantines_and_preserves_original_mapping`; `::test_reconcile_quarantines_bad_gate_and_continues` | H08-OPS records distinct IDs/counts and every unresolved alias. |
| A03 Source-specific choices | `src/asks/sources.py`, gate/proposal command ports; source decision fences (92) | `tests/test_ask_sources.py::test_declined_gate_keeps_waiters_and_reopen_advances_generation`; `::test_proposal_decision_is_atomic_and_receipt_is_replayed`; complete gate/routing area checks | H08-OPS verifies declines and technical waits remain held. |
| A04 One winning decision | `src/asks/service.py`, `buttons.py`; immutable decision generation/idempotency constraints (91), audience/root/mode token fences (93) | `tests/test_ask_decisions.py::test_concurrent_decisions_reserve_one_generation`; `::test_idempotency_key_rejects_changed_payload`; `tests/test_discord_ask_buttons.py::test_concurrent_choices_return_one_winning_receipt`; `::test_actor_and_exact_destination_binding`; `::test_stale_buttons_apply_nothing_and_refresh` | H08-OPS races authorized surfaces in a test project. |
| A05 Exact head/wait | `src/asks/sources.py` integration and wait adapters; exact source receipts (92) | `tests/test_integration_records_and_gates.py::test_ask_integration_decision_pins_current_head_and_journal`; `::test_gate_answer_cannot_be_reused_for_a_moved_head_or_generation`; `tests/test_ask_sources.py::test_human_resume_command_consumes_exact_wait_receipt` | H08-OPS records source generation/head evidence. |
| A06 Question/recovery authority | Question/recovery adapters in `sources.py`, checked domain ports; claim/attempt identity and source receipt fences (92) | `tests/test_agent_question_end_to_end.py::test_expired_claim_cannot_accept_a_prepared_question`; `::test_codex_question_survives_restart_and_answer_waits_for_draft`; `tests/test_escalation_commands.py::test_required_human_question_uses_verified_reply_not_supervisor_text`; `::test_task_recovery_refuses_ambiguous_reply_without_source_action` | H08-OPS verifies one terminal injection and current recovery attempt. |
| A07 Unified intake | `src/asks/intake.py`, gateway boundary, input identity uniqueness (93) | `tests/test_ask_intake.py::test_two_pending_asks_and_yes_clarify_without_deciding`; `::test_clear_explicit_intent_applies_once_as_stored_human`; `::test_gateway_backfill_replay_and_conflict_have_one_owner`; `::test_conditional_approval_saves_formal_feedback_and_folds_in`; `::test_legacy_thread_window_one_move_notice_and_rollback` | H08-OPS tests bound/ambiguous replies and records move notice; H09-OPS verifies expiry. |
| A08 Durable discussion | `src/asks/discussion.py`, reused correspondence and conversation inputs (91/93) | `tests/test_ask_discussion.py::test_discussion_is_atomic_replayable_and_revision_bound`; `::test_answer_scope_promotion_and_no_decision`; `::test_offline_delay_uses_delivery_facts_and_elapsed_time` | H08-OPS samples visible agent attribution and delayed answers. |
| A09 Complete feedback | `src/asks/review.py`, `coverage.py`, obligations and pinned packets (91) | `tests/test_review_feedback.py::test_packet_keeps_every_revision_response_and_verbatim_note`; `tests/test_ask_operations.py::test_external_coverage_precedes_review_gate_release`; `tests/test_ask_migration.py::test_historical_feedback_is_reported_without_reapproval` | H08-OPS audits predecision comments, including author responses, against downstream work. Missing historical access is explicitly unverified. |
| A10 Serialized feedback | Source-row-first locks in review/service mutations; feedback version and reserved packet fence (91) | `tests/test_review_feedback.py::test_mutations_wait_for_source_then_refuse_reserved_packet`; `::test_stale_feedback_preview_and_caller_authority_are_refused`; `tests/test_ask_decisions.py::test_packet_and_projection_lock_source_before_ask` | H08-OPS checks another-tab/Discord invalidation. |
| A11 Recoverable application | `src/asks/operations.py`, `src/reviews/service.py`; operation keys/dependencies/leases and obligation mappings (91) | `tests/test_ask_operations.py::test_filing_failure_retains_review_hold_and_retries_once`; `::test_crash_after_task_insert_keeps_application_gate_until_finalize`; `::test_mapping_transaction_rolls_back_task_and_retries`; `::test_event_failure_preserves_seq_and_one_ingestion_intent`; `::test_mapping_loss_refuses_gate_release` | H08-OPS records pending/failed/unknown source reconciliation; no inferred success. |
| A12 Pinned ingestion | Review application, immutable approval snapshots and dispatch; `ensure_spec_ingest`; reviewed default-pipeline bundle | `tests/test_spec_approve_command.py::test_standalone_snapshot_pins_bytes_provenance_and_same_body_reuses_decision`; `tests/test_ask_operations.py::test_post_acceptance_vault_edit_is_preserved_for_repair`; `tests/test_default_pipeline_intake.py::test_review_approval_ingests_once_at_deep_high`; `::test_dispatch_retries_failed_run_until_transactional_task_receipt`; complete bundle fixture check | H08-OPS verifies installed bundle/profile grants before enabling ingestion. |
| A13 Fenced transport | `src/asks/delivery.py`, `transport_controls.py`, `reconcile.py`; mode/binding/outbox/attempt schema (93) | `tests/test_ask_delivery.py::test_unknown_create_survives_restart_without_repost`; `::test_expired_lease_cannot_overtake_late_old_edit`; `::test_deleted_root_replaces_once_and_retires_tokens`; `::test_cutover_refuses_unverified_inventory_or_outstanding_http`; `tests/test_ask_migration.py::test_import_keeps_proven_receipt_and_unknown_notifier_history` | H08-OPS fences both admissions, reconciles actual HTTP receipts, disables old controls and redirects aliases before channel CAS. |
| A14 Pause/fairness | Shared delivery scheduler plus project/source checks; persisted attempts unaffected by pause (93) | `tests/test_ask_delivery.py::test_pause_before_claim_and_immediately_before_send_consumes_no_attempt`; `::test_shared_scheduler_delivers_with_digest_disabled_and_routine_fairness`; `tests/test_discord_ask_buttons.py::test_pause_does_not_disable_valid_decision_or_resume_project`; `tests/test_agent_question_end_to_end.py::test_question_answer_while_paused_does_not_resume_worker` | H08-OPS verifies pause/resume with still-current requests. |
| A15 Scope/retention | `src/asks/authority.py`, gateway admission, discussion retention and source-bound decision evidence (91/93) | `tests/test_ask_intake.py::test_queued_agent_answer_rechecks_public_audience`; `tests/test_ask_discussion.py::test_retention_keeps_promoted_words_but_expires_ordinary_chat`; `tests/test_ask_api.py::test_gateway_tools_cannot_be_called_by_http_or_mcp`; legacy-thread rollback test above | H08-OPS verifies allowlist removal/private scope; rollback cannot admit a new external decision. |
| A16 Shared count/surfaces | Canonical query and digest identity hash; `dashboard/src/components/asks/`, Focus/Reviews/ActivityDrawer | `tests/test_ask_commands.py::test_report_and_digest_share_audience_count_and_identity_hash`; `::test_missing_projection_reports_refreshing_not_empty`; FocusInbox/FocusAsk/FocusReview, ReviewsInbox and ActivityDrawer.review Vitest files | H08-OPS compares live canonical IDs, distinct counts and degraded reads. |
| A17 Draft/preview/browser | Review Discuss/Comments/DecisionPreview and AskDecision components | `dashboard/src/panes/review/__tests__/index.test.tsx`, `DiscussionPanel.test.tsx`, `DecisionPreview.test.tsx`; `dashboard/src/components/asks/__tests__/AskDecision.test.tsx`; `dashboard/layout-checks/checks/human-asks.check.mjs` (phone and desktop: keyboard discussion/save, draft and pending-save hold, full mapping, stale feedback event and one decision) | H08-OPS records actual narrow-screen/keyboard sample; browser stub proves UI behavior, not production transport. |
| A18 Migration/cleanup/rollback | Migrations 91/92/93; resumable source/history cursor in existing `system_config`; `src/doctor/ask_checks.py`; delivery_disabled revokes external tokens | `tests/test_migration_human_asks.py::test_upgrade_from_90_and_replay_preserve_history`; `::test_fresh_metadata_matches_upgrade_and_replays`; `::test_decisions_refuse_unsafe_downgrade_and_audit_mutation`; `::test_transport_upgrade_from_historical_outbox_preserves_receipts`; `tests/test_ask_migration.py::test_bounded_history_scan_preserves_action_evidence`; `::test_cutover_stages_cas_and_rollback_do_not_restore_legacy`; receipt import/doctor test above | H08-OPS owns production inventory/transfer. H09-OPS owns elapsed 30-day window, receipt parity, final cleanup and installed-content evidence. |

## Schema and retained history

The actual sequential revisions are `a00000000091_human_ask_decisions.py`,
`a00000000092_ask_source_fences.py`, and `a00000000093_human_ask_transport.py`.
The source-fence migration consumed the provisional transport ID 92. Cleanup must
allocate a later free ID and must never rewrite these deployed revisions.

Source/history cursor batches default to 100. Dry runs roll back their projections
and cursor; apply commits both together. Historical correspondence and actions
keep IDs, verified actors, text, incident/task snapshots, original result and unknown
status. Root imports retain receipt provenance; absent delivery proof produces an
unknown binding and case. A notifier cursor is never delivery proof. Report-only
historical feedback cases have recorded human dispositions and cannot reapprove,
create obligations, release gates or erase outstanding writes.

Doctor refuses cleanup for transfers, unresolved operations/writes, unmapped input,
open conflicts, missing receipt provenance or an unelapsed/unstarted compatibility
window. Its clean result is a necessary ledger check, not H09-OPS approval.

## Required validation commands

Use the disposable PostgreSQL test DSN and repository resource gate:

```bash
aq test tests/test_ask_sources.py tests/test_ask_decisions.py tests/test_review_feedback.py tests/test_review_service.py tests/test_review_commands.py tests/test_review_queries.py tests/test_review_gating.py
aq test tests/test_task_batch_commands.py tests/test_default_pipeline_intake.py tests/test_spec_approve_command.py tests/test_ask_operations.py
aq test tests/test_gate_queries.py tests/test_routing_gate_guard.py tests/test_integration_records_and_gates.py tests/test_playbook_v2_commands.py tests/test_playbook_wait_repository.py tests/test_agent_question_end_to_end.py
aq test tests/test_ask_intake.py tests/test_ask_discussion.py tests/test_discord_ask_buttons.py tests/test_discord_inbound_router.py tests/test_escalation_commands.py tests/test_conversation_queries.py tests/test_conversation_outbox.py tests/test_conversation_maintenance.py
aq test tests/test_ask_delivery.py tests/test_escalation_delivery.py tests/test_delivery_integration.py tests/test_digest_supervisor.py tests/test_digest_queries.py
aq test -m migration tests/test_migration_human_asks.py
aq test tests/test_ask_commands.py tests/test_ask_api.py tests/test_ask_migration.py tests/test_reviewer_api_scope.py
aq test tests/test_default_pipeline_intake.py tests/test_playbook_v2_fixtures.py
scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh
alembic heads
scripts/regenerate-generated.sh --check
npm -w dashboard run typecheck
npm -w dashboard run build
npm -w dashboard run check:layout -- --only human-asks
npm -w dashboard test -- src/pages/focus/__tests__/FocusInbox.test.tsx src/pages/focus/__tests__/FocusReview.test.tsx src/pages/focus/__tests__/FocusAsk.test.tsx src/pages/reviews/__tests__/ReviewsInbox.test.tsx src/panes/review/__tests__/index.test.tsx src/panes/review/__tests__/DiscussionPanel.test.tsx src/panes/review/__tests__/DecisionPreview.test.tsx src/shell/__tests__/ActivityDrawer.review.test.tsx src/components/asks/__tests__/AskDetail.test.tsx src/components/asks/__tests__/AskCard.test.tsx src/components/asks/__tests__/AskDecision.test.tsx
```

The e2e home, database, port and tmux socket must be disposable and isolated.
The focused frontend set is the 11 files under Focus, Reviews, review pane,
ActivityDrawer.review, and shared AskCard/AskDetail/AskDecision referenced above.
Task comments and the final close receipt record exact commands, outcomes and
baseline/environment exceptions; do not replace missing evidence with a pass.

## H08 local validation receipt — 2026-10-11

On the converged H06/H07 tree plus H08, the eight test groups above passed with
221, 115, 131, 127, 123, 13, 112 and 45 tests respectively. The additional
`aq test tests/test_doctor.py tests/test_selection_catalogue.py` passed 139 tests.
The 11 frontend files passed 61 tests; typecheck, build and both phone/desktop
browser checks passed. Generated-artifact drift and changed-file Ruff checks
passed. `python -m alembic heads` reported only `a00000000093`; this equivalent
invocation replaced an installed `alembic` wrapper with a broken interpreter path.

The isolated swarm smoke passed S1–S20, using its own home, database, port and
tmux socket. Tier 1 does not exercise real providers, external messages or actual
integration delivery; those exclusions remain explicit in its receipt. The
operator database and daemon were not modified. Local logs and browser artifacts
are under `/tmp/aq-h08/`; durable task comments/close evidence identify the final
pushed commit. No production historical sample or cutover receipt is claimed.
