# Remaining legacy consumer retirement — 2026-10-12

Task: `bright-torrent-72`; downstream C2: `calm-zenith-98.2` (paused).

## Decision and revision identity

**The final eligible table-drop subset is empty.** This task deletes the remaining
dormant development publisher/adapter and its enabling transfer command. It does
not authorize dropping historical recovery state or active repair evidence. No schema,
migration, production database operation, rollout gate, stage budget or CI requirement
changes here. Jack's authorization supersedes the old observation/date/release holds;
the exclusions below follow live consumers and safety contracts rather than those holds.

Source ancestry: delivered `dev`
`322b4bde54ac85e69d1e38c92ed3adf79cb9b9d6` was merged with C1
`e5f47414495b7fbd0f637fe6b87906c6dfb91dd8` through merge
`f12adee458312aa883710324531da7b3f8613d23` (not cherry-picked). This report and its reference inventory belong to the
single implementation commit on
`aq/epic/retire-remaining-dormant-legacy-consumers-before-c2-table`.
The exact final full SHA is recorded in the task close evidence and companion vault
audit after commit. Until configured integration delivers it, that SHA is a worker
checkpoint, not delivered-default evidence. C2 must revalidate against that delivered
revision before designing any drop. The inventory also hashes every source file with an inventoried reference and the startup/helper boundary so the audited source content can be checked independently of commit identity.

## Startup and command reachability

With `integration.git_first=active`, [core startup](../../src/orchestrator/core.py#L2026)
constructs retained `DevelopmentPrimitives` (Git, cleanup and repair helpers), then
[`train_for` and `TrainCommandDriver`](../../src/orchestrator/core.py#L2047).
The old development subject runtime is explicitly `None` at line 2061. Startup
maintenance still includes cleanup, branch discard/materialization, owner recovery
and [`RepairService.retire_terminal_delegates`](../../src/orchestrator/core.py#L2073).
Neither deleted adapter/runtime was constructed, registered or ticked by this path.

The active train imported only three helpers from the former runtime:
`development_repository`, `project_development_pin`, `load_pinned_development_policy`.
The first now lives in [repository_git](../../src/integration/repository_git.py);
the latter two in [development_policy](../../src/integration/development_policy.py).
Their repository configuration, frozen regeneration settings, reviewed source hash
validation and retained artifact/vault fallback contracts are preserved. The train
and integration CI model import those retained locations directly.

The registered internal `integration_release` command still reaches
[`IntegrationReleaseService`](../../src/commands/integration_commands.py#L1921) through
[its handler](../../src/commands/integration_commands.py#L2162), with promotion-intent,
candidate-revision and schedule/lease/replay reads. Registered repair start, timeout,
dispatch and close handlers still reach [RepairService](../../src/commands/integration_commands.py#L2275).
The factory is not a scheduled legacy publisher, but these admitted recovery contracts
mean their tables cannot be dropped in C2. Active repair stages still read/write
`dossier` and insert stage evidence in [repair.py](../../src/integration/repair.py#L1591).
Neither that column nor `integration_repair_stage_evidence` is a drop candidate.

Archival [task-reference discovery](../../src/database/queries/task_references.py#L114)
queries parent episodes/verifications. Cleanup [protected refs](../../src/integration/delivery_branches.py)
and [legacy retirement refusals](../../src/integration/legacy_park_retirement.py#L56)
consult historical subjects. Retained [engine ownership](../../src/integration/engine.py#L271)
checks unresolved subjects and journal publication ambiguity. Those are safety/history
consumers, not evidence that the removed development publisher is running.

## Removed paths and retained boundary

Removed `src/integration/development_runtime.py` and
`src/integration/development_adapter.py`, including their development admission,
subject frontier/scheduler, writer projection, primitives adapter and engine transfer
implementation. Removed `integration_development_engine_transfer` from handler,
typed contract, CLI and generated command inventories. Its generated reference page
and fifteen orphaned reference pages for C1-removed commands are deleted using the
registry-derived orphan inventory; historical rollout reports/specs remain unchanged. The old
`development-engine-transfer` CLI can no longer dispatch. There were no startup
callers to migrate beyond the three helpers above.

Removed tests that exclusively exercised those dormant publishers; retained reviewed
policy, trusted exact-head/gate, historical-subject cleanup and loader integrity checks.
Import quarantine tests reject both deleted modules and exercise the retained train
entry points; the active-startup test also quarantines both during real service
construction and the CommandHandler tick. Operator guidance now uses current recovery controls rather than
C1-deleted candidate/rebind publishers.

Retained generic historical `engine-transfer`, release/repair commands, cleanup fences,
recovery records and archival references. Removed module names in import-quarantine
ratchets are intentional negative references. A synthetic Git-conflict fixture in
`tests/test_integration_train_sources.py:4450` uses a former runtime filename as
arbitrary test data, without importing or invoking it. Historical rollout ledgers,
reports and dated specs retain their evidence. The exhaustive inventory below distinguishes
references from reachability: table declarations, legacy import support and uncalled
historical modules are not counted as proof of a running publisher.

## Table disposition and consumer evidence

Every table below is excluded from C2 in this revision. `active` identifies a current
train, task, startup-maintenance or admitted repair consumer. `retained` identifies
an admitted historical control, recovery, cleanup or archival contract. `protected`
marks the six explicit task exclusions; they must remain regardless of other findings.
Representative source links are paired with the reachability roots above; all literal
source reference sites and content hashes are in
[the machine-readable inventory](2026-10-12-legacy-consumer-references.json).

| Table | Classification | Retained consumer / reason |
| --- | --- | --- |
| `integration_source_ci` | active | Exact-head source CI consumed by the train and CI observation commands. [integration/train_sources.py:264](../../src/integration/train_sources.py#L264), [database/queries/task_references.py:152](../../src/database/queries/task_references.py#L152) |
| `task_integration_checkpoints` | active | Task completion, admission and train source identity. [integration/repair.py:6670](../../src/integration/repair.py#L6670), [commands/task_changes.py:175](../../src/commands/task_changes.py#L175) |
| `task_branch_origins` | active | Task branch identity, train admission and branch cleanup. [integration/train_sources.py:1815](../../src/integration/train_sources.py#L1815), [integration/delivery_branches.py:482](../../src/integration/delivery_branches.py#L482) |
| `integration_branch_owners` | protected | Active train/repair ownership and stopped-writer recovery fences. [integration/repair.py:6491](../../src/integration/repair.py#L6491), [integration/release.py:622](../../src/integration/release.py#L622) |
| `integration_review_evidence` | protected | Current review approvals and exact-head review proof. [integration/train_sources.py:1705](../../src/integration/train_sources.py#L1705), [commands/integration_commands.py:2532](../../src/commands/integration_commands.py#L2532) |
| `integration_root_authorizations` | retained | Registered root authorization and review-policy proof. [integration/reviews.py:373](../../src/integration/reviews.py#L373), [integration/root_authorization.py:119](../../src/integration/root_authorization.py#L119) |
| `integration_promotion_intents` | retained | Release and repair commands inspect intents; cleanup preserves unresolved ownership. [integration/repair.py:6686](../../src/integration/repair.py#L6686), [integration/release.py:549](../../src/integration/release.py#L549) |
| `task_delivery_receipts` | retained | Archival, hierarchy and removal guards preserve delivery history. [integration/repair.py:6218](../../src/integration/repair.py#L6218), [integration/release.py:589](../../src/integration/release.py#L589) |
| `integration_batches` | protected | Current Git-first train batches and controls. [integration/train_sources.py:2205](../../src/integration/train_sources.py#L2205), [integration/repair.py:6590](../../src/integration/repair.py#L6590) |
| `integration_batch_members` | protected | Current train membership, promotion and dependency proof. [integration/train_sources.py:1091](../../src/integration/train_sources.py#L1091), [integration/repair.py:5910](../../src/integration/repair.py#L5910) |
| `integration_candidate_revisions` | retained | Registered release/repair commands inspect historical candidates. [integration/repair.py:6106](../../src/integration/repair.py#L6106), [integration/release.py:513](../../src/integration/release.py#L513) |
| `integration_candidate_member_results` | retained | Release/repair validation and historical published handoff. [integration/repair.py:5895](../../src/integration/repair.py#L5895), [integration/release.py:572](../../src/integration/release.py#L572) |
| `integration_candidate_publications` | retained | Attestation, release and cleanup retain publication proof. [integration/repair.py:495](../../src/integration/repair.py#L495), [integration/release.py:520](../../src/integration/release.py#L520) |
| `integration_candidate_resolutions` | retained | Release/cleanup and removal guards preserve unresolved resolutions. [integration/release.py:818](../../src/integration/release.py#L818), [integration/cleanup.py:1095](../../src/integration/cleanup.py#L1095) |
| `integration_candidate_ref_mutations` | retained | Ownership/release/cleanup preserve ambiguous remote mutations. [integration/repair.py:4895](../../src/integration/repair.py#L4895), [integration/release.py:813](../../src/integration/release.py#L813) |
| `integration_root_intent_members` | retained | Release and cleanup resolve historical intent membership. [integration/release.py:580](../../src/integration/release.py#L580), [integration/cleanup.py:873](../../src/integration/cleanup.py#L873) |
| `integration_repair_operations` | active | Registered repair lifecycle and startup delegate retirement. [integration/repair.py:6820](../../src/integration/repair.py#L6820), [integration/release.py:760](../../src/integration/release.py#L760) |
| `integration_parent_episodes` | retained | Archive task-reference discovery and repository removal guards. [database/queries/task_references.py:153](../../src/database/queries/task_references.py#L153), [api/target_scope.py:243](../../src/api/target_scope.py#L243) |
| `integration_child_dispositions` | retained | Historical disposition/read projections and recovery records. [integration/observe.py:1474](../../src/integration/observe.py#L1474), [integration/parent_subjects.py:485](../../src/integration/parent_subjects.py#L485) |
| `integration_repair_stages` | active | Repair lifecycle owns the dossier, budgets, evidence and delegate handoff. [integration/repair.py:6529](../../src/integration/repair.py#L6529), [integration/release.py:541](../../src/integration/release.py#L541) |
| `integration_delegate_releases` | retained | Stopped delegate release/handoff recovery proof. [integration/delegate_release.py:364](../../src/integration/delegate_release.py#L364) |
| `integration_owner_recoveries` | active | Startup maintenance and stopped-writer recovery/cleanup. [integration/cleanup.py:1121](../../src/integration/cleanup.py#L1121), [database/queries/task_recovery_queries.py:1096](../../src/database/queries/task_recovery_queries.py#L1096) |
| `integration_check_evidence` | protected | Current exact-head checks and CI proof. [integration/train_sources.py:1632](../../src/integration/train_sources.py#L1632), [integration/repair.py:6679](../../src/integration/repair.py#L6679) |
| `integration_attestation_publications` | active | Attestation publication plus repair/release recovery proof. [integration/repair.py:3257](../../src/integration/repair.py#L3257), [integration/release.py:823](../../src/integration/release.py#L823) |
| `integration_cleanup_items` | active | Scheduled and commanded branch cleanup state. [integration/cleanup.py:1472](../../src/integration/cleanup.py#L1472), [commands/integration_commands.py:1969](../../src/commands/integration_commands.py#L1969) |
| `integration_repair_stage_evidence` | active | Repair lifecycle inserts evidence and attestation reads stage evidence. [integration/repair.py:1781](../../src/integration/repair.py#L1781), [integration/attestation.py:769](../../src/integration/attestation.py#L769) |
| `integration_parent_verifications` | retained | Archive task-reference discovery, hierarchy and delivery-truth history. [database/queries/task_references.py:114](../../src/database/queries/task_references.py#L114), [database/queries/integration_train_queries.py:91](../../src/database/queries/integration_train_queries.py#L91) |
| `integration_parent_operation_completions` | retained | Delivery-truth/hierarchy and historical drain-owner recovery. [database/queries/integration_train_queries.py:88](../../src/database/queries/integration_train_queries.py#L88), [integration/delivery_truth.py:598](../../src/integration/delivery_truth.py#L598) |
| `integration_episode_receipt_acceptances` | retained | Historical record/parent projections preserve accepted receipt proof. [integration/observe.py:1525](../../src/integration/observe.py#L1525), [integration/parent_subjects.py:439](../../src/integration/parent_subjects.py#L439) |
| `integration_parent_verification_evidence` | retained | Historical verification/CI evidence remains referenced. [integration/ci.py:1911](../../src/integration/ci.py#L1911), [integration/parent_subjects.py:528](../../src/integration/parent_subjects.py#L528) |
| `integration_operation_artifact_pins` | retained | Repair and artifact reachability preserve pinned policy history. [integration/repair.py:702](../../src/integration/repair.py#L702), [database/queries/integration_state_queries.py:315](../../src/database/queries/integration_state_queries.py#L315) |
| `epic_dependencies` | active | Current task graph integration dependency frontier. [task_graph/integration_dependencies.py:52](../../src/task_graph/integration_dependencies.py#L52) |
| `project_integration_schedules` | retained | Registered release command serializes schedule/CAS and quiescence. [integration/release.py:774](../../src/integration/release.py#L774), [database/queries/integration_reconciliation_queries.py:62](../../src/database/queries/integration_reconciliation_queries.py#L62) |
| `project_integration_leases` | retained | Release/repair/mutation recovery and historical retirement ownership fence. [integration/repair.py:4559](../../src/integration/repair.py#L4559), [integration/release.py:675](../../src/integration/release.py#L675) |
| `integration_release_results` | retained | Registered release idempotent replay and historical drain-owner state. [integration/release.py:767](../../src/integration/release.py#L767), [integration/drain_owners.py:88](../../src/integration/drain_owners.py#L88) |
| `integration_history_waivers` | retained | Cutover/history control queries retain operator waiver decisions. [database/queries/integration_control_queries.py:203](../../src/database/queries/integration_control_queries.py#L203) |
| `integration_rollout_transitions` | retained | Cutover/history control queries retain rollout transition proof. [database/queries/integration_control_queries.py:206](../../src/database/queries/integration_control_queries.py#L206) |
| `integration_history_waiver_consumptions` | retained | Cutover/history control queries retain waiver-consumption proof. [database/queries/integration_control_queries.py:254](../../src/database/queries/integration_control_queries.py#L254) |
| `integration_legacy_gate_applicability` | retained | Cutover/history control queries retain gate applicability. [database/queries/integration_control_queries.py:263](../../src/database/queries/integration_control_queries.py#L263) |
| `integration_legacy_suppression` | retained | Cutover/history control queries retain suppression decisions. [database/queries/integration_control_queries.py:337](../../src/database/queries/integration_control_queries.py#L337) |
| `integration_outbox` | active | Startup outbox dispatch and policy/command artifact reachability. [integration/repair.py:3382](../../src/integration/repair.py#L3382), [api/target_scope.py:263](../../src/api/target_scope.py#L263) |
| `integration_outbox_artifact_pins` | active | Outbox and artifact retention keep exact dispatch policy pins. [database/queries/playbook_artifact_queries.py:569](../../src/database/queries/playbook_artifact_queries.py#L569), [integration/outbox.py:263](../../src/integration/outbox.py#L263) |
| `integration_legacy_deliveries` | protected | Read-only historical delivery proof used by the train. [integration/train_sources.py:320](../../src/integration/train_sources.py#L320), [integration/delivery_branches.py:985](../../src/integration/delivery_branches.py#L985) |
| `integration_subjects` | retained | Protected branch refs, live-subject retirement refusal and engine-transfer fencing. [integration/delivery_branches.py:475](../../src/integration/delivery_branches.py#L475), [integration/engine.py:344](../../src/integration/engine.py#L344) |
| `integration_subject_journal` | retained | Engine-transfer publication ambiguity and gate/operator-decision recovery proof. [integration/engine.py:89](../../src/integration/engine.py#L89), [database/queries/integration_subject_queries.py:386](../../src/database/queries/integration_subject_queries.py#L386) |

## Verification and handoff requirements

Focused checks cover reviewed policy/loader integrity, retired command transport,
contract registration, import quarantine, active train imports and historical-subject
cleanup refusal. Area checks cover train sources, batches, jobs, CI producers,
cleanup, archive, repair, ownership and offline CLI startup. Generated artifacts,
selection catalogue, changed-file Ruff and whitespace checks are recorded with exact
commands and outcomes in task evidence and the companion audit. Unavailable checks
are not passes; no whole-suite result is claimed.

The first focused run found one fixture accidentally removed during retirement and
three stale CLI/documentation expectations inherited from C1. The fixture was restored
so the historical live-subject refusal still executes; stale expectations were replaced
with transport-refusal tests and current controls. No test budget or exclusion changed. The area run additionally found four
stale selection-rule paths for C1-deleted modules (`promotion`, `main_promotion`,
`review_evidence`, `candidates`). Those dead mappings are removed and the moved
repository helper is mapped to development-delivery checks; coverage validation
and active-startup/CLI checks are rerun after that correction.

C2 receives this branch/full SHA and the audit after publication. All 45 audited tables
remain, including the six protected tables, repair dossier/evidence and recovery/history
records. A future nonempty drop set requires an explicit replacement/retirement of
these consumers and a new delivered-revision audit. No vacuous drop migration is supplied.

Canonical AQ record search and create both returned `record.forbidden` for this worker
despite advertised grants. No knowledge-graph save or ingestion is claimed. The task
requires a separate repository/vault audit artifact; it is retained as such, and the
canonical-storage limitation is reported to the supervisor with the handoff.

### Recorded checks

Tests use the repository-supported disposable PostgreSQL server via
`POSTGRES_TEST_DSN`; worker/operator database refusal sentinels are untouched.

- `aq test tests/test_development_subjects.py tests/test_cli_integration.py tests/test_lifecycle_cleanup.py -q`: **115 passed** after the fixture/CLI corrections.
- `aq test tests/test_integration_train_sources.py tests/test_integration_batches.py tests/test_jobs.py tests/test_integration_ci_producers.py tests/test_integration_cleanup.py tests/test_archive.py tests/test_integration_repair.py tests/test_integration_repair_rollover.py tests/test_integration_repair_lineage.py tests/test_integration_ownership.py tests/test_integration_replay.py tests/test_integration_contracts.py tests/test_integration_train.py tests/test_cli_startup_offline.py tests/test_generated_artifacts.py tests/test_selection_catalogue.py -q`: **1,258 passed, six skipped, one failed** (the four stale C1 selection-rule paths above); 867.77 seconds. Targeted verification after the mapping fix is recorded below and in task-close evidence.
- `python scripts/generate-selection-catalogue.py`: regenerated 903 modules across 96 areas.
- `./scripts/regenerate-generated.sh` and `./scripts/regenerate-ts-client.sh --from-file`: completed successfully.
- `./scripts/regenerate-generated.sh --check`: generated artifacts current.
- `python scripts/gen-command-docs.py --check`: 184 command pages current after orphan removal.
- Changed Python paths: `git diff --name-only --diff-filter=AM | rg '\.py$' | xargs ruff check`: passed.
- `git diff --check`: passed.
- Source-ownership inventory: no unknown source files, unknown mutation sinks or budget errors; manifest and budget unchanged.
- Audit inventory: all 123 source fingerprints match; 45 table dispositions and 689 literal source/table reference pairs; eligible subset `[]`.
- `aq test tests/test_selection_catalogue.py tests/test_orchestrator.py::test_git_first_active_runs_the_train_instead_of_subject_runtimes tests/test_orchestrator.py::test_orchestrator_owns_single_integration_service_loop tests/test_cli_integration.py::test_operator_guide_uses_only_real_operational_commands_and_options -q`: **59 passed** in 38.75 seconds, including the corrected coverage ratchet and active startup quarantine.
