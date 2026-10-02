# Root integration adapters and engine ownership (phase 1)

Task `vivid-willow.5`, implementing `rev-agile-ridge` revision 2,
SHA-256 `5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`.
This handoff builds on the [subject contracts](2026-10-02-integration-subjects-foundation.md)
and [reviewed policy tables](2026-10-02-integration-policy-tables.md).

## Wiring and authority

`root_runtime.py` installs the observer, immutable policy loader and typed
command-backed ports at `IntegrationService`'s existing single remote-pass
boundary. Both `integration.reconciler_shadow` and `integration.reconciler_active`
default to `false`. Enabling a loop does not transfer publication authority.
Shadow visits only legacy subjects; active visits only reconciler subjects.
Shadow journals its proposed decisions and finite next visit, without calling
mutating ports or changing the subject's head, phase or engine. Its virtual
identity follows the current legacy batch revision and lifecycle.

Subject seeding is bounded and idempotent by the complete request key. It
requires an already imported root artifact with a compiled integration table;
old bundles stay on the legacy path. A new subject inherits the repository's
durable ownership, including after its previous subject finishes. Loading a
different project activation never changes a running subject's artifact pin.

`engine.py` excludes engine transfer from root mutations with PostgreSQL
repository advisory locks held through remote writes and read-back.
Operations hold a shared session lock on a dedicated autocommit connection;
transfer takes an exclusive transaction lock. Domain transactions still commit
before network requests. This
preserves concurrent preflight and journal recovery. Actual main writes also
take a separate, nonblocking repository publisher lock, shared by root promotion
and development delivery. Contention returns the existing blocked/busy outcome.
Legacy entry points consult durable ownership on every call, including on
restart. The development publisher's existing exclusion also takes this root
exclusion, so a mode change cannot start another main publisher. An unresolved
development publication event prevents transfer too. Nested command/service calls reuse the caller's exclusion; spawned
tasks cannot inherit that authority. Transfer checks the complete repository's
root subject/version map, journals its operator and evidence, and increments
each subject version in the same transaction. An unresolved promotion intent
or candidate mutation prevents transfer until existing recovery resolves it.

Active ownership reserves the actual default ref as a publisher under
`root-reconciler:<repository>`. The tested candidate is still identified by its
integration ref, exact SHA, generation and construction base. A candidate or
repair fence cannot be relabelled as a default-ref publisher fence. An absent
publisher fence is an unknown observation, including during shadow comparison.

## Phase-one ports

Every mutating port requires the exact committed active decision and current
subject version before invoking `CommandHandler`. Build, publication, repair,
ejection and cleanup continue through the existing commands and their journals.
Root publication additionally checks exact trusted green and the real publisher
fence. `RootPromotionService` remains the App-attestation, expected-old push,
ambiguous-write read-back and authoritative member-receipt implementation.

Receipt records bind the member's reviewed source head and the exact published
target, including its base. Attempts count a conclusive trusted observation of
one working writer head once per ordinal; reruns and new evidence IDs do not
reset that identity. Cancelled, infrastructure, untrusted and superseded results
do not count. The append-only attempt carries its projected count so a restart
between recording and subject CAS recovers it. Decision records must match the
committed rule, facts digest, primitive, pin, head and generation.

The root CI fallback uses `integration_build_candidate` and
`integration_ci_evidence`, the phase-one contracts named by the foundation.
The request port seeds a server observation after publication so absent evidence
does not prevent subsequent polling. Shared `CIAdapters` can be explicitly
supplied as `orchestrator.integration_ci_adapters`; their registered ports retain
the same exclusion, exact candidate check and decision prewrite. The shared CI
checkpoint was not an ancestor of this task base. Its owner must use the
observed candidate identity rather than compare the candidate ref to
`Subject.target_ref` (the publication destination) before selecting it here.

Repair filing checks the class, duration, attempt limit and whole-batch scope
against the frozen legacy boundary before starting a budget. Replaying an
existing stage dispatches it without restarting its clock. A successor reuses
the existing timeout/handoff only after stopped-writer proof and its original
absolute deadline; an unrepresentable request returns a bounded unknown.
Writer-stop proof and preservation use existing owner recovery, including the
`keen-stone-14` incident machinery. The root observer also reads the legacy
stage's own receipts as writer facts: an accepted handoff of the fenced ref, a
delegate-close or accepted-completion receipt for the latest stopped session,
adoption by a journalled merged build, and retirement before any claim
([acceptance scenarios](2026-10-02-root-reconciliation-scenarios.md)). Generic
writer ladders remain the phase-two writer owner's work. Cleanup refuses
mismatched retention/retry settings and uses existing independent cleanup
records. It also releases the batch's request and project lease (release folds
into primitive 20), so the next request can be sealed. Gate choices remain in the
pinned table; durable resolved gates are scanned so a lost wake only costs
latency.

## Operator cutover and rollback

These are operator actions after delivery and daemon update. A worker must not
run them against the operator database. This task does not enable either flag,
transfer a live repository, import a policy or provide the required production
shadow-week/scenario evidence.

The loops are installed at service startup. After changing these flags, the
operator uses `aq restart --no-dashboard`, which preserves live agent sessions,
then verifies health/readiness before continuing. A config value alone does not
install a loop into an already running service.

1. Import and review the root policy bundle through the existing playbook path.
   Set `integration.reconciler_shadow: true` and leave active disabled. Preserve
   the week of decision comparisons, exact artifact pins and scenario evidence
   required by revision 2 and `vivid-willow.6`. Review unknown observations,
   including unavailable publisher authority; they are not successful actions.
2. After the required approval, enable `integration.reconciler_active: true`.
   Legacy still owns the repository until explicit transfer. Obtain a fresh
   preview with `aq integration engine-transfer <repo> --engine reconciler --json`.
3. Apply the complete previewed subject map, repeating `--expected-subject` for
   every entry. Supply durable references to the reviewed evidence and approval:

   ```bash
   aq integration engine-transfer <repo> --engine reconciler --apply \
     --expected-subject <subject-id>:<version> --reason "approved root cutover" \
     --evidence <shadow-week-reference> --evidence <scenario-reference> \
     --evidence <operator-approval-reference>
   ```

   A changed subject map/version or unresolved write refuses the transfer.
   Re-observe the preview; do not replace an unresolved journal with a timeout.
4. To roll back, preview with `--engine legacy`, then apply the fresh complete
   subject/version map and a reason. This remains available when active is
   disabled. Transfer waits for an in-flight root action and refuses unresolved
   remote writes. Existing recovery must settle those under their original owner.
   Once transfer succeeds, disable active. Shadow may remain enabled for diagnosis.

Rollback retains every subject, policy pin, journal, candidate/preservation ref
and reservation row. The main publisher reservation becomes released; a later
activation gets a newer fence token. Disabling flags alone stops visits but
retains durable authority, so legacy cannot silently publish in that interval.

## Verification

`tests/test_integration_root_adapters.py` covers activation/rollback races,
restart ownership, inherited-task authority refusal, decision prewrite,
exact-green publication through the real promotion service, attempt replay and
crash recovery, shadow identities, frozen writer/cleanup contracts and the shared
CI registration seam. CLI transport is checked in `tests/test_cli_integration.py`.
The affected integration area and adjacent promotion/repair checks are recorded
on the task with their exact results. The operator's shadow week and live
cutover scenarios remain production gates, not worker test results.
