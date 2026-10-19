# Human asks: operator rollout and rollback

H08 delivers code readiness. H08-OPS owns production inventory and enablement;
H09-OPS owns the compatibility exit and cleanup. A passing local test is not a
production receipt. The approved specification is review `rev-sharp-zenith` r1,
implementing `rev-fair-ridge` r3. See the [acceptance manifest](../reference/human-asks-implementation.md).

Workers must not run production migrations, restart the daemon, enable destinations,
or manufacture human reconciliation decisions. Commands below belong to the local
operator, through CommandHandler. Worker and supervisor tokens cannot invoke them.

## Capture evidence before deployment

Create an operator-owned rollout record with this template. Record missing evidence
as missing; the historical production sample was unavailable during implementation.

| Field | Required evidence |
|---|---|
| Deployment | UTC timestamp, deployed full SHA, binary version, schema revision and single Alembic head |
| Scope | Project, destination `discord:<guild>:<channel>`, authorized audience, configured remote origin |
| Inventory bounds | Start/end UTC, maximum rows, actual rows, pagination cursors, skipped/failed reads |
| Sources | Review ID/revision/hash, exact gate `await_id`, explicit incident source, immutable proposal identity/digest, blocked waiter IDs |
| Delivery | Old delivery IDs, channel/root/thread IDs, stable markers, confirmed receipt time, unknown sends, active leases and attempts |
| UI parity | Distinct ask IDs and canonical Needs you count, verified aliases, blocking facts, digest hash, degraded sources |
| Historical audit | Decision time/actor, review revision, predecision comment IDs/text/author/anchor, author responses, downstream task IDs and acceptance mappings |
| Decisions | Operator identity, evidence reference/hash, case ID, disposition and timestamp |
| Cutover | Expected/returned mode generation, inventory hash, disabled-control and redirect evidence, canonical root selection |
| Compatibility | First channel switch timestamp, immutable 30-day deadline, move-notice receipts |

Use a bounded audit, initially the latest 100 approved reviews in the previous 30
days. Read all submitted comments up to each original decision, including comments
from earlier revisions and author-resolved rebuttals. Trace each actionable item to
its downstream work and acceptance evidence; absent linkage is **unverified**.
Record truncation and continue with a new explicit bound before claiming coverage.
A valid **none found** result records the query, time bounds, count of reviews and
comments examined, completed pagination, and zero qualifying rows with no failed
reads. Missing access, an empty first page, or an unavailable source is not none found.

`notified_revision` is a notifier cursor, never delivery proof. Neither an old
approved state nor an accepted reply proves application. Do not reapprove historical
reviews, change their bytes, invent new obligations, or infer success from age.

## Feedback safety and shadow sources

1. Deploy packet/feedback fencing for every approval caller. There must be no old
   approval writer alongside the new one. Pending application operations hold work.
2. Operator applies the forward migrations. Actual chain: ledger **91**, source
   fences **92**, shared input/outbox **93**. The design's provisional cleanup ID 93
   was consumed by transport; H09 must allocate the next free sequential revision.
3. Inspect `aq ask reconcile --help` and `aq ask cutover --help`. Run
   `aq ask reconcile --project-id PROJECT` (dry-run default). The result contains
   bounded source actions, conflicts, durable cursor position and inventory hash.
4. Apply with `aq ask reconcile --project-id PROJECT --apply --limit 100` until
   `cycle_complete` is true. Each batch commits its cursor with its projections.
   A crash before commit replays the batch; a crash after commit resumes the saved
   cursor. Dry runs never move it. The daemon also runs bounded scans, wrapping to
   catch source events missed during downtime. Do not run a shell polling loop.
5. Compare canonical IDs/counts and held waiter facts. Exact references coalesce
   review/gate/bound incident; titles never do. Conflicting aliases remain quarantined.
   Preserve old correspondence/action IDs, verified actors, archived title snapshots,
   original results and unknown status.
6. Advance `legacy -> shadow -> dashboard` with `aq ask cutover --project-id PROJECT
   --mode MODE --expected-generation N`. Read the current generation with
   `aq ask cutover-status --project-id PROJECT --json`: it lists each recorded
   destination and includes the configured destination as legacy/generation 0
   when absent. It also reports the migrated flag, first channel switch UTC and
   immutable compatibility deadline. This read changes no state. Use the configured
   destination's generation for the CAS; never guess or retry a stale CAS blindly.

Review each historical repair case. Record the operator's decision using
`aq ask reconcile --project-id PROJECT --apply --case-id CASE --disposition
reviewed_no_action --evidence 'record identity/hash and justification'`, or
`mapped_work` with the actual mapping evidence, or `retain_unknown` with explicit
provenance. These dispositions record evidence; they do not apply an action,
create obligations, approve a document, repair a conflicting alias, or erase a
transport fence. Resolve contradictions at their owning source and rescan.

## Transfer each destination

1. CAS `dashboard -> transferring`. This commits a new generation and stops both
   send admissions even when writes remain unresolved. Outstanding writes are
   retained. Expired leases do not prove an HTTP call stopped.
2. Run bounded reconciliation for this project. Proven old root receipts are imported
   into canonical bindings without sending. Conflicting roots and reviews lacking
   a proven receipt become unknown bindings and repair cases, never mass reposts.
3. Inspect `aq doctor --check asks.cleanup_ready` and reconciliation inventory.
   Reconcile legacy and shared unknown/sending operations through their owning
   ledgers. `aq ask retry-operation --decision-id ID` invokes source reconciliation;
   it cannot turn unknown into safe retry without source proof. For shared delivery,
   use `aq ask repair-delivery --delivery-id ID --expected-generation ROOT_GENERATION
   --disposition confirmed_sent --evidence 'receipt evidence'`. `confirmed_absent`
   requires verified deletion; `cancel` explicitly acknowledges irreconcilability
   and permits one replacement generation. It is not evidence the old send failed.
4. Select the proven canonical root. Disable old external controls and redirect
   alternate roots. Record actual receipt/marker/control evidence. Server-side old
   controls are fenced during transfer; a visible button alone grants no authority.
5. Rerun the dry inventory after writes are settled. CAS to `channel` using the
   returned `inventory_sha256`, `--legacy-controls-disabled`, `--aliases-redirected`,
   and `--evidence` naming the verified operator record. The command recomputes
   blockers and the inventory hash before storing the ownership generation.
   A boolean without matching inventory and evidence is refused. Only then can the
   shared pump publish asks. Keep general chat configuration independent.
6. Record the returned 30-day per-destination compatibility deadline. Authorized
   legacy-thread input uses the same dedup ledger, with one main-channel move notice
   per binding. At deadline it receives a pointer without decision application.
   Rollback and re-enable never extend this deadline. Historical links stay readable.

Do not remove old ledgers while transfers, unresolved operations, unmapped inputs,
open conflicts or unverified receipt migrations remain. Doctor reports blockers,
not an automatic destructive fix. Canonical unknown provenance survives cleanup;
retaining unknown does not reconcile an outstanding HTTP write. H09 additionally
requires elapsed compatibility and observed operational acceptance.

## Operational acceptance and rollback

H08-OPS records an authorized test-project sample: one new ask, one migrated root,
a deterministic decision, ambiguous text across two asks, discussion and conditional
review approval, comment-aware approval, pause/resume, deleted root and simulated
transport ambiguity. Verify one active root, one domain effect, no new threads,
matching distinct counts, full feedback mapping, and declines retaining holds.
Record unresolved conflicts and unknowns. H09-OPS records the final before/after
sample and installed profile/skill/playbook drift, reseed/merge and activation receipts.

Rollback is `aq ask cutover --project-id PROJECT --mode delivery_disabled
--expected-generation N`. It advances the generation, revokes external controls
and project intake, and pauses delivery even with unknown in-flight writes. Preserve
CLI/dashboard decisions, feedback, operations and configured general chat. Reconcile
already submitted requests; never restart legacy publishers. Re-enable only through
a fresh transfer. Database/binary downgrade requires a separately verified
forward-compatible reader; a delivery rollback is not a schema downgrade.
