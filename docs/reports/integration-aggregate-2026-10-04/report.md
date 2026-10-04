# P4 aggregate acceptance and remaining gates

Task `quick-glacier-88.4`; approved source `rev-agile-ridge` revision 2, SHA256
`5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`.
Measured implementation base: `6f1fb7ed7` (full OID in [inventory.json](inventory.json)).

The consolidated surface, incremental engines and verified retirement
mechanism are assembled. **Phase 4 completion is pending**: legacy deletion,
production cutovers and final exact-head full CI are external gates. This child
ships the measured reconciliation, local invariant checks and
[operator guide](../../guides/integration-reconciler-rollout.md).

Supervisor instruction `msg-b25198be2128469d935c65eb0f3efa0a`, received
2026-10-04, confirms: Jack owns shadow-week/cutover/flags and `quick-glacier-88.5`
deletion; this child closes with focused tests plus one area. Full CI is the P4
epic's aggregate verification on its exact head after collection: a verifier
plus the parent's required CI, then parent review and delivery with supervisor
authorization of its exact verified untyped root head. The epic
cannot finish before the deletion gate clears. Migration 64 remains unchanged.

## Measured targets

The read-only [inventory script](../../../scripts/acceptance/integration_simplification_inventory.py)
imports this checkout, never the installed daemon/CLI. It records file hashes,
all primitive outcomes, controls with their removal gates, doctor registrations,
tables and each family's remaining source references. Physical Python lines
include blanks/comments; modules include `__init__.py`.

| Measure | Revision 2 target | Aggregate source | Disposition |
| --- | --- | --- | --- |
| Integration Python modules | 9 | 113 | Pending consolidation/deletion. |
| Integration Python lines | Under 12,000 | 78,227 | Pending consolidation/deletion; wrappers still retain old engines. |
| Mechanism primitives | 20 | 20 | Closed enum matches the proposed names. This is a contract count, not proof that legacy adapters have disappeared. |
| Distinct primitive outcomes | About 45 | 48; 55 declared primitive/outcome pairs | Exact table labels are implemented; common labels overlap. Universal `unknown(reason)` is already among the 48. The approximate prose total is not a strict 45-code assertion. |
| Approved operator controls | 6, with `flush` also retained | 7 | Four decisions, two diagnostics and `flush`; §5.1's six excludes its HAND row. |
| Visible CLI entries | Six in phase-4 acceptance prose | 9 | Seven approved entries, `resolve-candidate-member` worker protocol and gated `legacy`; nested `gate answer`/`policy activate` are counted once each. |
| Doctor checks | 3 | 3 approved + 21 legacy = 24 registered | Legacy checks retain removal gates; includes delivered-parent check added after the review's 20-check inventory. |
| Integration-related tables | 9 named targets | 46 | 37 legacy tables in seven families + nine retained tables; the nine retained are **not** the spec's nine target tables. |

The proposed `integration_subject_members`, `integration_writers`,
`integration_attempts` and `integration_gates` tables are not separate tables
in this snapshot. `integration_subject_journal` is the current journal name;
writer/budget facts are also projected on `integration_subjects`, with existing
legacy evidence/member/gate substrates still in use. The nine retained tables
include two retirement audit tables and the two unresolved classifications
`task_integration_checkpoints` and `integration_review_evidence`. All seven
families have `code_removed=false` and all 37 tables still have source references.
The count must not be presented as successful nine-table consolidation.

There are 46 retained legacy control entries (including related controls
outside `aq integration`). They remain explicitly classified with replacements
and removal gates rather than disappearing from help and being called deleted.
The installed `aq integration --help` still exposes the old flat controls and
its `aq db retire --help` is unavailable; this is deployment skew, not a source
test pass or a reason to reseed the shared installation from a worker slot.

Two operator handoff gaps also remain in the source. Development's tested
`transfer_development_engine` has no CLI/CommandHandler registration;
`development_engine_transfer` appears only as a journal label. The public
`integration_engine_transfer` handler selects root/parent ownership only.
Development adoption and rollback must wait for that supported command wiring,
rather than bypassing CommandHandler by calling a helper. Cross-phase finding
`vivid-delta-93` records it at project root behind triage gate
`gate-231c694533ae`, with provenance to this audit; it is a production cutover
prerequisite, not a request to widen the child reporting task. Additionally,
`policy activate --policy` retains the disabled/drained requirement for
non-development project settings; immutable subject artifacts alone do not
complete the proposed end-to-end no-drain policy edit surface.

Reproduce the measurement on a checked-out aggregate:

```bash
python scripts/acceptance/integration_simplification_inventory.py \
  --source-sha "$(git rev-parse HEAD)" > /tmp/integration-inventory.json
```

## Verification

Focused and area commands/results are retained in [checks.json](checks.json).
They use the documented disposable PostgreSQL service on port 5534. No operator
database, production project settings or feature flags are changed.
The focused run passed **51 tests**, including migration-chain retirement,
archive/restore, worker refusal, consolidated controls and subject holds.
The affected area passed **356 tests**. No local full suite was run.

The area includes root/parent/development real-Git scenarios, interrupted
publication, shared Git/CI/writer/receipt/gate primitives, App attestation and
policy compiler checks. It exercises exact trusted green, wrong-head/red
refusals, journal-before-push, expected-old/fenced writes, repository publisher
exclusion, immutable receipt replay, retained failed work and binding holds.
The parent suite reuses `keen-stone-14` recovery and covers feature-off ownership
and audited rollback; recovery is not recreated by this task.

Known-failure reference: the vault's
`projects/agent-queue/notes/full-suite-baseline-2026-09-22.md`, source main
`35d9605eab99480b29fd54598eb840b2a614ee09`, CI run `35806218584`. It is a
historical comparison, not evidence for this aggregate's full CI.

## Remaining gate ledger

| Gate | Owner | Evidence required | Current disposition |
| --- | --- | --- | --- |
| P0 observation / P1 shadow week | Jack / project supervisor | Recorded coverage and reviewed comparison, not elapsed time | Pending; prior initial report had no shadow observations and both flags off. |
| Root cutover | Jack | Exact imported artifact, shadow/scenario evidence, human approval and audited complete version-map transfer | Pending production receipt. |
| Parent cutover | Jack | Root receipt plus exact episode pin, migration, scenario, approval and transfer | Pending; disposable parent evidence exists. |
| Development per-project cutover | Jack | Parent gates plus each project's artifact/evidence and exclusive publisher transfer | Pending for `matter-engine-cpp` and `agent-queue-web`; disposable scenarios exist. |
| Development transfer command wiring | Project supervisor / implementation owner | Registered, scoped CommandHandler/CLI preview/apply route to the tested development transfer, including rollback | Missing in this source; live development transfers must wait. |
| Legacy engine/control/doctor/table deletion | Jack; `quick-glacier-88.5` | Completed cutovers, zero legacy obligations, source-reference/FK checks, backup and guarded retirement receipts | Pending; measured source still uses all 37 retirement tables. |
| Full hosted aggregate CI and delivery | P4 epic integration owner | Final collected head after deletion, complete required job green, trusted exact-head evidence and delivery receipt | Pending P4 aggregate verification; not this child's local job. |

Existing [P0](../2026-10-03-integration-stall-recovery.md),
[parent](../parent-rollout-2026-10-03/report.md) and
[development](../development-rollout-2026-10-03/report.md) evidence supplies
prior test/operator snapshots; it does not close the pending gates above.
Use the operator guide for automatic recovery, per-project policy changes,
additive migration, family retirement and rollback. Publication of this worker
checkpoint does not prove default-branch delivery or approve a production gate.
