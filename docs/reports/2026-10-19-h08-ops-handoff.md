# H08-OPS bounded preflight and operator handoff

Task: `vivid-pinnacle-60`, claim epoch 4. Evidence captured on 2026-10-19;
source verification completed at `2026-10-19T19:39:47Z`.

**Status: source inventory and execution packet prepared; live rollout and
30-day compatibility acceptance remain unverified.** No production mutation,
external decision, deployment, migration, or transport exercise was performed.

## Authority and ownership

Project-supervisor message `msg-07f60c601ef4433f870d6f278596f514` withdraws the
earlier engineering hold, citing global delegation
`msg-4c43f9741f374eaeaa0202ce8ae70e78`. This worker read the project message;
the global message is an attributed reference, not independently verified here.
The prior engineering-hold wait was cancelled. Engineering delegation does not
change the local-operator-only ask controls or authorize impersonating a human.

Actual runtime refusals:

| Attempt | Result | Routing |
|---|---|---|
| `aq ask reconcile --project-id agent-queue --no-apply --limit 100 --json` | Exit 4, `out_of_scope: ask gateway and operator controls are daemon/local only` | Local operator must run the bounded inventory. |
| Message to `session:supervisor-global` | Exit 4, `out of scope: system messages require global admin` | Project supervisor must forward the operator packet. |

Both refusals were reported once in `msg-6cc3017ddc3248b3a3261d994a6548da`,
thread `h08-ops-epoch4-operator-handoff`. No alternate identity, token, direct
database query, or wrapper was used to bypass them. The task comment immediately
corrected an earlier statement that the global handoff had been delivered.

The local operator owns live inventory, deployment identity, external actions,
reconciliation decisions and cutover. The project/global supervisors coordinate
that execution and downstream ownership. H08-OPS's current description still
requires live cutover and observed compatibility exit for a passing close;
publishing this packet does not satisfy that acceptance condition.

## Source and readiness receipts

Pinned source: `c8069197078550b5e940fcdf6061e573ceeac239`, observed as `origin/dev`.
Its commit timestamp is `2026-10-19T11:49:40-07:00`. This is a local remote-tracking
reference, not a deployed-binary receipt or a fresh network observation.
The held worktree initially remained clean at
`55af0ca8c086fbe19f6938fac578f77303c232ff`; source was read using `git show`.

| Evidence at pinned source | SHA-256 / result |
|---|---|
| `docs/guides/human-asks-rollout.md` | `abb7485ef698e70ba3e979d4e8d363524aeebd426ae1bb15d6b2025598a9f531` |
| `docs/reference/human-asks-implementation.md` | `db9735dc96cc80dcaa3e76cb3554536e9bff0aa6b2f50e55c1af85f202d1986e` |
| `a00000000096_human_ask_decisions.py` | `648a98601cb088c77494e0a03d8689a94ac7ce9984994782f901cbec9e253279`; parent 95 |
| `a00000000097_ask_source_fences.py` | `4a94b58ff01a1dcf67c1381c6beb8136a157f36def062b28d17243e15c22e65b`; parent 96 |
| `a00000000098_human_ask_transport.py` | `dffba0a97b804c500bda8110352756a77e9b6f4cea57e67ce80414821a4687f6`; parent 97 |
| `aq db current --json` | Exit 0; stamped `a00000000101`, head `a00000000101`, schema at head. Endpoint omitted from this redacted packet. |

The runbook still describes provisional revisions 91/92/93. The actual source
chain is 96/97/98; `bf9022432442c6af4178a595e447721f97694d50` updated the manifest
after the original H08 commit `f8b51ef15e902b40bb9e5ff9d7ab4706c7ee352f`.
Do not replay provisional migration numbers. The schema read does not prove
which binary the daemon is running, old-writer fencing, or production acceptance.

The manifest records historical local validation on the converged H08 tree:
eight backend groups (221/115/131/127/123/13/112/45), 139 doctor/catalogue tests,
61 frontend tests, build/typecheck/browser checks and isolated S1–S20 smoke.
Those are attributed historical receipts, not fresh tests on this source SHA.
An AST read against the pinned source verified 15 relevant test definitions
exist across the six modules named below. They were not executed in this task.
No code changed, so no code regression suite or production exercise was claimed.

## Missing live evidence and bounded inventory request

Capture one operator-owned, redacted evidence record with these fields before
instantiating any mutation below:

| Field | Current evidence / required return |
|---|---|
| Deployment | Full deployed SHA, binary version and observation UTC are missing; return them independently of `origin/dev`. |
| Scope | Project `agent-queue`; actual `discord:<guild>:<channel>`, audience/config fingerprint and remote origin are missing. Test-project identity is also missing. |
| Ownership | Current mode, monotonic generation, migrated flag, first switch UTC and compatibility deadline are missing. Do not assume absent row or generation 0. |
| Sources | Return exact review revision/hash, gate `await_id`, incident binding, proposal identity/digest and held waiter identities. |
| Delivery | Return proven root/thread IDs and receipts, aliases, active leases, sending/unknown operation IDs and attempted-write disposition. |
| Feedback | Audit latest 100 approved reviews within the previous 30 days; record fixed start/end UTC, all predecision comments including prior revisions and author responses, downstream mappings and pagination completion. |
| Parity | Return canonical ask IDs/counts, blocking facts, digest identity hash, degraded reads and unresolved cases. |

Exact initial local-operator reads:

```bash
aq db current --json
aq ask reconcile --project-id agent-queue --no-apply --limit 100 --json
aq doctor --check asks.cleanup_ready
aq ask list --project-id agent-queue --view needs_you --limit 100 --json
aq ask list --project-id agent-queue --view history --source-kind review --limit 100 --json
```

Record timestamps, exit status, row counts, returned cursor and hashes for each.
Continue pages only within the declared audit bounds; record truncation and
failed reads. Missing access is not a `none found` result. The ask history list
alone does not establish coverage of all historically approved reviews.

In the inspected source, dry reconciliation returns blockers and an inventory
hash, not a complete current-mode record. `asks.cleanup_ready` is report-only
and also cannot prove the current generation or observed compatibility. Obtain
that identity through the operator's supported inspection/evidence surface;
if unavailable, route that concrete inspection gap before attempting a CAS.
Never use a guessed generation to discover state through a mutation.

## Generation-fenced execution packet

The following are **templates for the local operator**, not executed commands.
`$observed_generation`, `$verified_inventory_sha256` and `$evidence_ref` must come
from current receipts for the exact project/destination. Each transition uses
the immediately preceding returned generation; never calculate it speculatively.

1. Verify the deployed source includes packet/feedback fencing for every review
   caller and no old approval writer is callable. The source manifest names
   the relevant review, gate, proposal, repair and standalone-spec paths.
   Record actual installed profile/playbook/UI evidence. Schema-at-head alone
   is insufficient; any required deployment/migration remains operator-owned.
2. Reconcile bounded batches with
   `aq ask reconcile --project-id agent-queue --apply --limit 100 --json`.
   Record each cursor/receipt; resume until `cycle_complete` rather than using
   a shell polling loop. Apply may route inputs and enqueue discussion notices:
   it is an operational mutation, not a harmless inventory read. Historical
   cases require an attributed reconciliation decision; no invented obligations.
3. Only if the observed mode is `legacy`, advance to `shadow`; then, after
   source/count/held-work parity, advance `shadow` to `dashboard`:

   ```bash
   aq ask cutover --project-id agent-queue --mode shadow --expected-generation "$observed_generation" --json
   aq ask cutover --project-id agent-queue --mode dashboard --expected-generation "$observed_generation" --json
   ```

   These are separate steps with a fresh returned generation between them.
   Already migrated destinations must follow their actual current state.
4. Fence admissions with
   `aq ask cutover --project-id agent-queue --mode transferring --expected-generation "$observed_generation" --json`.
   Reconcile all outstanding HTTP writes and import proven roots. Lease expiry
   is not proof a remote request stopped. Record canonical root selection,
   disabled old controls and redirected aliases. Inspect shared-destination
   peers: source code also revokes modes whose config fingerprint changed.
5. Rerun the dry inventory. With no blockers other than the transfer itself,
   verified controls/aliases and evidence for this exact inventory:

   ```bash
   aq ask cutover --project-id agent-queue --mode channel --expected-generation "$observed_generation" --inventory-sha256 "$verified_inventory_sha256" --legacy-controls-disabled --aliases-redirected --evidence "$evidence_ref" --json
   ```

   Persist the returned destination, mode generation, switch evidence and immutable
   compatibility deadline. Stale CAS or changed inventory requires inspection;
   do not blindly retry. Repeat per authorized destination only.
6. Rollback, when required, is the following exact command shape using the then
   current generation:

   ```bash
   aq ask cutover --project-id agent-queue --mode delivery_disabled --expected-generation "$observed_generation" --json
   ```

   It revokes external tokens/intake and pauses delivery while preserving the
   CLI/dashboard decision boundary and unknown write evidence. Never revive
   legacy writers or infer schema downgrade safety. Re-enable through a fresh
   transfer; preserve the first compatibility deadline.

## Authorized test-project matrix

Every row below is **not executed live**. Record test project, UTC, actor,
source/version, input and decision IDs, operation receipt, root/generation,
counts and before/after evidence. Use authenticated human actions where required.

| Scenario | Required observed outcome | Source test reference |
|---|---|---|
| New ask and proven migrated root | One active root each; retained receipts/aliases; no new threads | `test_ask_migration.py::test_import_keeps_proven_receipt_and_unknown_notifier_history` |
| Deterministic choice and replay | One source effect and replayed input/decision identity | `test_ask_intake.py::test_clear_explicit_intent_applies_once_as_stored_human` |
| Ambiguous text with two asks | Clarification, no choice applied | `test_ask_intake.py::test_two_pending_asks_and_yes_clarify_without_deciding` |
| Discussion and conditional approval | Feedback preserved and owned, no unconditional approval | `test_ask_intake.py::test_conditional_approval_saves_formal_feedback_and_folds_in` |
| Comment-aware approval and stale preview | Complete prior-revision/author-response mapping; stale preview refused | `test_review_feedback.py::test_packet_keeps_every_revision_response_and_verbatim_note`; `::test_stale_feedback_preview_and_caller_authority_are_refused` |
| Pause/resume | Paused send consumes no attempt; current decisions do not implicitly resume work | `test_ask_delivery.py::test_pause_before_claim_and_immediately_before_send_consumes_no_attempt` |
| Deleted root | One replacement generation; old tokens retired | `test_ask_delivery.py::test_deleted_root_replaces_once_and_retires_tokens` |
| Simulated transport ambiguity | Unknown persists without repost; transfer blocked without proof | `test_ask_delivery.py::test_unknown_create_survives_restart_without_repost`; `::test_cutover_refuses_unverified_inventory_or_outstanding_http` |
| Decline and surface parity | Declined work remains held; shared count/identity hash agrees | `test_ask_sources.py::test_declined_gate_keeps_waiters_and_reopen_advances_generation`; `test_ask_commands.py::test_report_and_digest_share_audience_count_and_identity_hash` |
| Compatibility and rollback | One move notice; no deadline extension or revived legacy writer | `test_ask_intake.py::test_legacy_thread_window_one_move_notice_and_rollback`; `test_ask_migration.py::test_cutover_stages_cas_and_rollback_do_not_restore_legacy` |
| Historical unresolved feedback | Report original identities; no reapproval without recorded disposition | `test_ask_migration.py::test_historical_feedback_is_reported_without_reapproval` |

At each actual per-destination deadline, use an assigned durable timer or operator
schedule, then inspect real legacy replies: pointer only, no decision application,
historical links preserved, ledger parity and unresolved ownership evidence
accounted for. A timer firing is not acceptance. No deadline can yet be calculated
from available evidence. H09 cleanup must retain these actual prerequisites.
