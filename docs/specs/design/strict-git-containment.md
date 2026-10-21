# Strict Git containment: shadow contract and consumer inventory

Implementation of fast-smooth-integration revision 2, sections 2, 3, 11 and 14,
for `stark-willow-95.1`. The approved authority is
`projects/agent-queue/specs/2026-10-12-fast-smooth-integration.md` in the operator
vault. This is the section 2 consumer inventory supplement, not cutover approval.

## Contract

`await delivery_truth.delivered(request, target_ref, observation=snapshot)`
accepts an immutable `DeliveryRequest` for the current accepted completion.
`DeliveryAnswer` carries the scoped generation, retained source OID, full target
ref/OID, collector UTC timestamp, observation token, contract version, state,
stable reason and a safe diagnostic. Its states are `contained`, `pending`,
`unknown` and `not_applicable`. It has no policy-level `satisfied` flag.

The observation comes from `GitTruth.snapshot`, which fetches all branch and
provenance refs once, outside SQL locks. All tasks and target projections in that
observation share its token and timestamp. With no observation, the result is
`unknown / observation_unavailable`; the reader never derives transport or
repository authority from a bare task ID. Diagnostic owners must discard expired
observations, as `DeliveryObserver` already does. A known freshness failure is
`unknown / observation_stale`, including when an OID proof is cached.

Only replacement-disabled `merge-base --is-ancestor` of the exact original
source and observed target can return `contained`. Equality uses the same
object-validated primitive. A successful negative is `pending`; shallow
negatives, missing objects/provenance/target or Git errors are `unknown`.
No trailer, patch ID, tree comparison, merge-noop, replacement ledger, settlement,
receipt, status or CI verdict can supply the positive answer. A reverted source
remains contained by ancestry. A deleted source branch is immaterial when its
immutable completion marker retains the original object.

A retained `artifact:false` marker preserves the existing explicit no-code
verification and produces `not_applicable`. A non-Git organizational completion
requires explicit `code_free_proven`; mere branch absence is insufficient.
A Git no-change source equal to its recorded base is tested by ancestry like any
other source. Failed and unfinished outcomes cannot satisfy admission. Verified
parent generations keep their verified-operation identity and source fence.

`DeliveryDisposition` models `delivered_by_other_means` separately. The cutover
owner authorizes/writes it using `integration_delivery_disposition` task metadata.
It binds project, repository, task, completion, target, original retained OID,
content-carrying OID, historical equivalence evidence and the cutover plan digest.
It cannot alter the strict state; `DeliveryAnswer.disposition` exposes it only
when the generation/target/source fences match. Lifecycle consumers may retire
that obligation, and metrics exclude it; neither can count it as containment.
The cutover owner must preserve this audit through archive and rollback.

## Shadow compatibility and mutation fences

Default runtime construction remains `legacy-equivalence-v1`. This task changes
no project configuration, daemon activation, publication policy or receipt writer.
Strict observations are explicitly constructed with
`GitTruth(git, contract_version=CONTAINMENT_CONTRACT_VERSION)` for shadow callers.
Their existing `is_delivered`, `contains_source` and `DeliverySnapshot.evaluate`
entry points delegate to the new contract/strict primitive. Any observation can
also use `delivered` or `strict_is_delivered` without changing its legacy readers.
`GitProvenance.contained(..., contract_version='strict-ancestry-v1')` delegates to
`strictly_contained`. Legacy proof implementations are temporarily retained and
must be removed by the fenced cutover and final retirement tasks below.

Bounded immutable-OID facts include store/repository identity, repository URL,
source OID, target OID and proof contract version. Strict full-answer caches also
bind every request field and pinned provenance marker OIDs. Cached metadata and
object validation never authorize a new generation or target. Failed proofs are
not cached. Cached answers refresh their collector timestamp/token for each new
observation. Overlapping fetch sharing and bounded failure backoff remain intact.
Cold `cached_only` reads cannot launch Git or turn missing evidence into a result.

Callers gather requests and Git facts before row locks. Before claim activation,
parent publication or epic completion, they reload completion/graph/policy and
target bindings and compare the complete request, frozen source and observation
identity. They use `is_fresh` and expected-old publication. A remote rewrite
invalidates subsequent evidence; a historical positive cannot authorize use of a
new tip. S1 supplies this contract; admission and cutover tasks own wiring/fences.

## Section 2 receipt consumer inventory

Source inventory at the S1 task base. Paths below include active/shadow and
historical recovery seams; none is silently declared inactive merely because the
usual live path uses Git. Owners are section 13 graph keys: `admission`, `cutover`,
`close_merge`, `dispatch`, `fixers`, `watchdog`, `dashboard`, and `policy_docs`.
The final `policy_docs` retirement ratchet must cover all entries before activation.

| Source | Receipt role / authority to remove | Removal owner |
|---|---|---|
| `src/database/queries/hierarchy_queries.py` | Sibling prerequisite fallback, delivered-head query, mutation/removal guard | admission |
| `src/database/queries/claim_queries.py` | Frontier fallback and attempt-progress inference from code/noop receipt | admission; policy_docs for attempt accounting |
| `src/database/queries/integration_train_queries.py` | Root receipt predicate, pending requests and delivered-source lookup | dispatch; admission |
| `src/database/queries/integration_delivery_queries.py` | Promotion finalizer receipt read/insert; public receipt listing | close_merge; policy_docs |
| `src/database/queries/blocked_state.py` | Blocked-state projection reads landed root/parent receipts | admission |
| `src/task_graph/readiness.py` | Readiness reads delivery receipts for sibling/parent landing | admission |
| `src/database/base.py` | Receipt-list query protocol | policy_docs |
| `src/commands/surface_commands.py` | Task delivery display via receipt-list API | dashboard; policy_docs |
| `src/commands/integration_commands.py` | Compatibility integration delivery/status commands | cutover; policy_docs |
| `src/doctor/stall_checks.py` | Completed-undelivered fallback without an observer | watchdog; admission |
| `src/integration/child_delivery.py` | Matching receipt, reissue writer and old parent-episode eligibility | watchdog; close_merge; policy_docs |
| `src/integration/records.py` | RECORD_RECEIPT writer and disposition receipt writer | close_merge; policy_docs |
| `src/integration/runtime_contracts.py` | Receipt primitive/type registry and observation facts | policy_docs |
| `src/integration/collection.py` | Collection receipt readers and receipt chain | close_merge; policy_docs |
| `src/integration/hierarchy.py` | Delivered head and reparent/collection guards | admission |
| `src/integration/observe.py` | Receipt table snapshots, child selection, parent readiness | admission; close_merge; policy_docs |
| `src/integration/parent_subjects.py` | Parent receipt table augmentation and generation facts | close_merge; policy_docs |
| `src/integration/parent_source.py` | Receipt source/ancestor locator and already-delivered guard | admission; policy_docs |
| `src/integration/root_pull_requests.py` | Root readiness/diagnostic receipt exclusion | dispatch; admission |
| `src/integration/epic_delivery.py` | Shadow receipt projection, delivered display and stale receipt selection | dashboard; admission |
| `src/integration/source_repairs.py` | Repair ancestor delivery using a root receipt | fixers; admission |
| `src/integration/repair.py` | Conflict-resolution receipt identity/chain and current receipt inventory | fixers; close_merge; policy_docs |
| `src/integration/parent_repair_heads.py` | Receipt-covered extensions and resolution receipt validation | fixers; close_merge; policy_docs |
| `src/integration/detached_repair_rebind.py` | Indirect repair receipt inventory and rebind proof | cutover; policy_docs |
| `src/integration/cancelled_collection_recovery.py` | Historical cancelled collection receipt chain | cutover; policy_docs |
| `src/integration/failed_verification_recovery.py` | Historical verification receipt inventory | cutover; policy_docs |
| `src/integration/suspended_parent_recovery.py` | Historical contiguous receipt-chain recovery | cutover; policy_docs |
| `src/integration/stranded_children.py` | Matching receipt and missing-receipt recovery selection | watchdog; cutover; policy_docs |
| `src/integration/drain_owners.py` | Receipt-backed owner release | admission; policy_docs |
| `src/integration/removal_guard.py` | Receipt fallback for hierarchical archive/delete | admission |
| `src/integration/cleanup.py` | Batch receipt cleanup gate and descendant receipt projection | admission; dispatch; policy_docs |
| `src/integration/release.py` | Terminal batch release proof from receipt ordinals/reservations | dispatch; policy_docs |
| `src/integration/cutover.py` | Receipt inventory, target retagging writer and trigger controls | cutover |
| `src/integration/task_deletion.py` | Receipt snapshot for historical deletion audit | policy_docs; retain audit without delivery authority |
| `src/integration/truth_reconciler.py` | Idempotent historical receipt after fresh Git verification and retained immutable witness; records first observation, never receipt-based delivery authority | watchdog; policy_docs; retain audit without delivery authority |
| `src/integration/legacy_backfill.py` | Historical delivery attestation/relanding and receipt-based source locator | cutover; policy_docs |
| `src/integration/shadow.py` | Indirect selected_receipt_id comparison against Git | cutover; policy_docs |
| `src/integration/promotion_contracts.py` | Promotion receipt IDs in historical result contract | policy_docs; retain recovery identity if needed |
| `src/database/tables.py` | Receipt schema, foreign keys, receipt primitive enum | policy_docs; separately gated post-rollback migration |
| `src/database/legacy_sqlite_import.py` | Offline historical import table inventory | policy_docs; historical import is not runtime authority |

### Admission consumer disposition (S4)

`AdmissionView` freezes eligible IDs, reasons and hierarchy evidence once per
request. Scheduler, pool demand, task explain, dependency readiness and verified
wait admission project that view. Claims gather fresh evidence before locks and
recheck graph, route, completion generation, repository, target and project policy
under their existing claim fences. Configured pool bounds and warm floors remain
capacity policy, separately represented from eligible task demand.

The admission-owned receipt fallbacks in `hierarchy_queries`, `claim_queries`,
`blocked_state`, `task_graph/readiness`, `hierarchy`, `removal_guard`,
`source_repairs`, `drain_owners` and `doctor/stall_checks` are removed. SQL blocked
state projects source completion for `delivery-of`; the view separately proves
current default-target containment. A missing truthful observer withholds only
Git-gated candidates and cannot restore receipt eligibility. Cleanup materializes
historical resource identities without treating receipt presence as delivery and
revalidates current default-target proof before destructive legacy cleanup.

Shared inventory entries remain explicit transition work: `integration_train_queries`
and `root_pull_requests` still contain legacy root selection/diagnostic receipt
reads owned jointly with dispatch; `observe` and `parent_source` retain the old
parent episode/recovery protocol owned with close_merge/policy_docs;
`epic_delivery` retains the shadow display projection owned with dashboard. These
paths are not declared inactive or retired by S4. Their owning stages must remove
the remaining authority before the final policy_docs retirement ratchet permits
activation. Receipt IDs retained on cleanup items and failed delivery diagnostics
are historical operation identity, never positive eligibility proof.

Actual SQL writes are `integration_delivery_queries._finalize_integration_promotion_on`,
`records.RecordsPrimitives.record_receipt_on`, `records.ParentEpisodeRecords.record_disposition`,
`child_delivery.ChildDelivery._reissue_receipt`, `truth_reconciler.GitTruthReconciler._repair`,
and `cutover` receipt retagging.
The records class names are described by their methods; future retirement must
remove the write statements and their command/primitive routes, not just readers.
S1 does not invoke or change these writes. Historical tables/foreign keys remain
until rollback retention and separately approved destructive migration permit deletion.

`main_promotion.py`, named in the approved spec's baseline inventory, is absent in
this checkout. Promotion runtime uses `promotion_steps.py` and train publication;
there is no file to retire under the old name. `parent_adapters.py` has no receipt
read/write at this base. Unrelated knowledge request receipts, playbook step receipts
and CI attestations are separate contracts and are not delivery proof.

## Equivalent-proof and retained-source consumers

| Source | Existing role | Successor owner |
|---|---|---|
| `src/integration/git_truth.py` | AQ-Source trailers, whole-source patches, equal trees, merge-noop in is_delivered/contains_source | truth supplies strict delegates; cutover selects; policy_docs removes legacy paths |
| `src/integration/delivery_truth.py` | Replacement-backed evaluate and SETTLED satisfaction | truth supplies strict reader; admission separates lifecycle retirement |
| `src/integration/provenance.py` | Replacement ledger traversal and completion/replacement writers | truth supplies strict ancestry; cutover retains original OIDs; policy_docs retires equivalent delivery authority |
| `src/integration/legacy_backfill.py` | Equal-tree/content attestation and historical replacement provenance | cutover dispositions equivalent-only rows |
| `src/integration/provenance_migration.py` | Immutable source/provenance namespace migration | cutover retains this locator, never interprets it as delivery |
| `src/integration/source_trailer.py`, `src/integration/gitops.py` | Diagnostic source trailers on real merges | policy_docs removes trailer proof readers; provenance text may remain diagnostic |
| `src/integration/train_sources.py` | Train snapshots, frozen input proof and leased publish checks | dispatch; close_merge |
| `src/integration/delivery_observer.py` | Batched consumer view and legacy/active projections | admission; watchdog; dashboard |
| `src/integration/delivery_branches.py` | Branch deletion delivery guard via common view | admission |
| `src/integration/stacked_branches.py` | Frozen prerequisite source proof and prepared base checks | admission; stacking |
| `src/integration/admission.py` | Train/admission policies over delivery proofs | admission |
| `src/integration/epics.py` | Epic readiness and frozen collection inputs | dispatch; epic_ci_advisory |
| `src/orchestrator/core.py`, `src/integration/cleanup.py` | Default GitTruth construction | cutover selects strict construction under fenced activation |

No receipt backfill, dual write, live policy switch, operator database migration,
project reconfiguration or retrospective SLO claim is authorized by S1.
