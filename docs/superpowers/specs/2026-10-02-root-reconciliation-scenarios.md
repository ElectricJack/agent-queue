# Root reconciliation acceptance scenarios (phase 1)

Task `vivid-willow.6`, implementing `rev-agile-ridge` revision 2,
SHA-256 `5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`,
§6 phase 1 acceptance. It builds on the
[subject contracts](2026-10-02-integration-subjects-foundation.md), the
[policy tables](2026-10-02-integration-policy-tables.md) and the
[root adapters](2026-10-02-root-integration-adapters.md).

## What the scenarios prove

`tests/test_integration_root_scenarios.py` runs the shipped root wiring over real
Git (a bare origin with every update of `main` kept in its ref log) and a
disposable PostgreSQL database:

- `RootSubjectRuntime` with both loops (active and shadow) on the daemon's
  `IntegrationService` tick, beside the legacy service sources and the schedule
  pass that renews the project lease. The root-train playbook rules are disabled.
- `RootObserver` with an exact Git port, `PinnedRootPolicy` over the shipped
  `agent-queue-root-train` artifact and the real `agent-queue-train-policy.json`
  project policy, and `RootPrimitiveAdapters` over a real `CommandHandler`. Only
  the GitHub transport is a fixture: the CI producer feeds the real `CIService`,
  the App attestation proof is supplied to the real `RootPromotionService`, and
  ref deletion and PR closing go to the fixture origin and forge.

Legacy seals the first batch and the shadow loop journals its mirror without
mutating anything. The operator's audited `engine-transfer` (with evidence) is
the only operator action. After it, the tests play the outside world only:

1. **Section-6 root scenario.** Three sources share revision `a00000000002` and
   all edit `base.txt`, so two of them conflict with the first at once. The
   reconciler builds, records the conflict and leaves the filed writer to work.
   The writer resolves the whole batch and re-chains the migrations, publishes
   through its own fenced resolve path, and the accepted handoff lets the
   reconciler rebuild before the writer's close is accepted (closed writer).
   Red candidate CI makes the reconciler file the successor writer itself. That
   writer pushes in place and closes. The reconciler rebuilds generation 1, observes exact
   trusted green, publishes it through the `root-reconciler` publisher fence,
   cleans up, releases the request and seals, builds and publishes the next
   batch on promoted main.
2. **Never-claimed writer.** A project policy variant authored as reviewed
   Markdown (same compiler, no Python) replaces the capacity wait with an
   ejection of the earliest conflicting member once an unclaimed writer has
   spent its budget. Both conflicting members are ejected after the 1800 s
   primary budget, and `main` takes the exact green candidate well within twice that
   budget. No session ever runs.

Both tests assert the invariants: every command is one of the phase-one adapter
commands and follows its committed active decision; each `git_publish` has its
fenced prewrite (owner `root-reconciler:<repo>`, `require_green`) before the
push; `main` only ever held exact candidates with conclusive trusted green
evidence from the frozen producer and check set, each pushed from its recorded
expected-old base; the red head never reached `main`; delivery receipts bind
the exact reviewed heads. Held (`hold:` label), rejected and ejected sources keep
their branch, open PR, review and labels, and gain no delivery receipt.

## Fixes the scenarios required

Each fix below is load-bearing: reverting any one makes a scenario stall
(checked by mutation).

- `observe.py`: before construction a root batch has no candidate or head, so
  every member reported `ancestry_unknown` and the table's `unknown-facts` rule
  waited forever. Members now relate to the publication target's observed tip.
  An unsealed root's frontier holds (a paused or rejected source) no longer hold
  the whole train; they stay out of the seal and keep their branch. Sealed
  members and a subject's own task still bind it.
- `root_runtime.py` (`RootObserver._legacy_writer`): the legacy stage keeps
  naming its writer after the work is done, and the table rebuilds only on
  `writer_status=stopped`. The root observer now reads the stage's own receipts.
  An accepted repair that moved the fenced ref to the collector stops the
  writer. So does a delegate-close or accepted-completion receipt for its latest
  stopped session with no locked checkout. A stopped writer that a journalled
  merged build adopted is no longer the subject's writer. A writer retired
  before any claim never ran. A writer that still holds its ref, is attached,
  or has no receipt keeps the observer's answer and still needs owner recovery.
- `root_adapters.py`: primitive 20 folds in release (§3.4), so `cleanup` now calls
  `integration_release`. Without it the request and project lease stayed held
  and no next subject was ever seeded.
- `integration_policy.py`: the derived projection `s.conflict_member` names the
  earliest conflicting member in manifest order. Lists are not addressable by
  path, so a table can only express "eject the conflicting member" through it.
- `candidates.py` (pre-existing, both engines): an ejection reserves the next
  revision and reseals the batch. A rebuild that conflicted again lost its
  conflict CAS, which requires `building`. Starting that reserved revision now
  moves `sealed` to `building`, as a fresh reservation does.

## Not covered here, and follow-ups

- The production seal (`MigrationInspector`) defers colliding migrations to
  later batches. The scenario keeps them together, as the continuous-delivery
  fixture did, so one writer re-chains them.
- The shared `CIAdapters._matches` compares the candidate head with
  `Subject.head` (the publication ref). It is not in this base and was already
  reported by `vivid-willow.5`; the scenarios use the root CI fallback.
- Ejection runs under the reconciler's default principal, so its audit event
  names `human:local-operator`, not the policy decision (`sharp-forge-57`).
- A claimed writer whose candidate ref already moved beyond its stage start
  (the builder's partial publication) reads `working` before it pushes. Only
  attempt counting could be affected, and CI is never observed in that phase
  (`smart-journey-77`).

## Operator handoff

Nothing here enables a flag, imports a bundle or transfers a live repository.
These scenarios are the real-Git/PostgreSQL evidence `vivid-willow.7` and the
cutover runbook cite (`--evidence root-scenarios:<pushed commit>`). The
shadow week, its decision comparison and the explicit approval remain
production gates. The operator commands, restart and rollback are unchanged
from the [root adapters handoff](2026-10-02-root-integration-adapters.md#operator-cutover-and-rollback).
