# Task 9b2 fix 1a report

## Identity and scope

- Fix base: `d20f2c13f72557e43afe2200b0f2953a1bf78365`.
- Runtime/tests commit: `464730aaaedd9ecbce32cc61ca3e21185a5f9b36`.
- Read `task-9b2-review.md` and the binding `task-9b2-fix-1a-brief.md`
  completely before editing.
- This correction is limited to the three Critical root-main reconciliation
  findings, authenticated descendant import, and the mandatory pre-main
  attestation boundary. It does not change the root receipt/member schema,
  finalizer completeness checks, SQLite receipt triggers, or event shapes owned
  by fix 1b.
- No Task 10 publisher/playbook/workflow, Task 11 enablement, live network,
  credential, forge, operator-database, or project-enablement work was performed.
- No schema or migration changed. The attestation identity is frozen in the
  existing immutable root-intent provenance, so dual-dialect migration testing
  is not applicable to this fix.

## Delivered corrections

### Root-main claim isolation

- `CandidateService` now recognizes an explicit closed set of candidate-owned
  mutation purposes. A reserved or applied `root_main` mutation is a blocker;
  candidate build/rebuild never observes, marks, rotates, deletes, or supersedes
  it.
- `RootPromotionService` remains the sole root-main reconciler. If an older
  candidate observer already left the claim `applied` while the root intent is
  still `prepared`, authenticated remote proof advances the intent to `pushed`
  and continues the atomic root finalizer instead of stranding the promotion.
- The cross-service crash regression pushes main, stops before root proof, calls
  public candidate build and rebuild, verifies the root claim/intent are
  unchanged, then restarts root reconciliation. The original intent produces
  both receipts with exactly one main push.

### Prewrite ambiguity and execution horizon

- Claim takeover and owned-claim renewal require `prewrite_at IS NULL`. Once the
  prewrite marker exists, an authenticated main read may finalize a proven
  candidate/descendant, but every other observation is
  `reconciliation_blocked`; no successor repeats the write.
- The claim horizon is 135 seconds for a bounded 120-second Git operation plus a
  five-second prewrite margin. An owned unmarked claim is renewed before use.
- Prewrite marking is one hierarchy-locked transaction that rechecks the exact
  current root snapshot, immutable intent authority, injected attestation proof,
  project-lease horizon, claim nonce/state/horizon, and null marker before
  setting `prewrite_at`. No provider or Git operation runs under that lock.
- The regression uses a genuinely overlapping fake write. After nominal claim
  expiry, a second service returns bounded `reconciliation_blocked` and the
  transport attempt count remains one until the first writer is released.

### Moved-main liveness and public rebuild

- Current main movement waits while the exact claim is live. After exact expiry,
  only an unmarked claim with the unchanged observed nonce can atomically move
  the claim and intent to `superseded` and the batch from `promoting` back to the
  existing public candidate-building lifecycle.
- Marked, live, unknown-ancestry, or stale-observation cases remain blocked and
  cannot erase a successor claim.
- The regression calls public `CandidateService.rebuild(batch, 0, moved_sha)`;
  that API creates revision 1, preserves the ordered sealed members, accepted
  repair-stage attempts/deadline, and rebinds the exact structured batch subject.
  Neither runtime nor test directly writes `current_revision`.

### Authenticated descendant import

- A main OID different from both the expected old SHA and tested candidate is
  imported with Task 9a's App-auth exact-OID fetch into
  `refs/aq/root-main-observed/<intent-id>` in the daemon-retained store before
  local ancestry proof.
- Installation-token acquisition, exact fetch, and Git ancestry are outside SQL
  transactions. The subsequent applied-proof transaction re-locks the hierarchy
  and exact root intent/claim identity before changing durable state.
- Failed import, changed returned identity, or indeterminate ancestry fails
  closed as `reconciliation_blocked`.
- The real-Git regression constructs the descendant only in a separate bare
  origin, proves it is initially absent from the retained store, imports and
  pins it through the exact-fetch adapter, then proves candidate ancestry and
  finalizes without a push.

### Mandatory attestation boundary

- Added strict frozen `RootAttestationSubject` and `RootAttestationProof` types.
  The subject binds numeric and full repository identity, operation, batch,
  revision, candidate SHA, and required-check version. The proof additionally
  requires a positive numeric check-run ID and canonical attestation external ID.
- Root prepare accepts only an injected/server-owned resolver returning the exact
  typed proof. Missing provider, dict/prose, exceptions, stale check version,
  wrong repository identity, wrong operation/revision, or wrong SHA returns
  `configuration_blocked` before recovery pin, root intent, mutation claim,
  authenticated main read, or push.
- The exact proof is stored in immutable intent provenance and compared again
  against a hierarchy-locked current snapshot. Immediately before a possible
  write, the resolver is called outside SQL and the exact frozen proof is
  revalidated again inside the prewrite transaction.
- After authenticated main movement, recovery uses the immutable frozen proof
  and does not call the attestation resolver or initiate another CI/audit
  observation. Task 10 remains responsible for publishing and authentically
  resolving the proof.
- The command's default service wiring takes only the orchestrator-owned
  `integration_attestation_resolver`; there is no permissive default or
  caller-supplied proof surface.

## TDD evidence

### Critical 1: purpose isolation and prepared/applied recovery

- RED:
  `pytest -q tests/test_integration_main_promotion.py::test_candidate_service_cannot_consume_root_claim_after_main_push`
  failed after the candidate observer consumed `root_main`; the candidate path
  then raised `candidate repair budget could not be activated` and stranded the
  prepared root intent.
- GREEN: the same command returned **1 passed, 3 warnings in 1.25s**.
- Historical prepared/applied replay:
  `pytest -q tests/test_integration_main_promotion.py::test_root_reconcile_repairs_applied_claim_with_prepared_intent`
  returned **1 passed, 3 warnings in 1.19s**.

### Critical 2: prewrite and horizon

- RED:
  `pytest -q tests/test_integration_main_promotion.py::test_expired_inflight_prewrite_is_blocked_until_remote_proves_result`
  showed the successor entering a second push while the first remained in
  flight.
- GREEN: the same command returned **1 passed, 3 warnings in 1.20s**; the
  successor completed boundedly with one transport attempt total.
- The fresh-horizon regression initially failed at `_mark_prewrite` because the
  original reservation no longer covered the required transport horizon.
  `pytest -q tests/test_integration_main_promotion.py::test_owned_unmarked_claim_renews_full_horizon_before_prewrite`
  returned **1 passed, 3 warnings in 1.18s** after owned-claim renewal was added.

### Critical 3: current moved main and rebuild

- RED:
  `pytest -q tests/test_integration_main_promotion.py::test_current_moved_main_expires_then_public_rebuild_creates_next_revision`
  observed `base_moved` while the current intent/claim remained reserved, so
  public rebuild could not create N+1.
- GREEN: the same command returned **1 passed, 3 warnings in 1.21s** after the
  exact expired/unmarked supersession transition was added.

### Important: exact descendant import

- RED:
  `pytest -q tests/test_integration_main_promotion.py::test_authenticated_descendant_is_imported_before_real_ancestry_proof`
  returned `reconciliation_blocked` because the authenticated descendant object
  was absent from the retained store.
- GREEN: the same command returned **1 passed, 3 warnings in 1.18s** and the
  intent-specific ref resolved to the exact descendant SHA.

### Attestation boundary and post-main behavior

- Initial RED was a collection error because `RootAttestationProof` did not
  exist.
- Exact proof gate:
  `pytest -q tests/test_integration_main_promotion.py -k 'root_prepare_requires_exact_server_attestation_before_claim or attestation_is_frozen_and_revalidated_before_prewrite or root_command_cannot_bypass_attestation_proof or authenticated_descendant_is_imported_before_real_ancestry_proof'`
  returned **10 passed, 3 warnings in 2.45s**.
- Post-main recovery initially returned `configuration_blocked` because it tried
  to resolve a fresh proof after main had already moved. After ordering frozen
  proof recovery before the prewrite-only resolver check:
  `pytest -q tests/test_integration_main_promotion.py::test_post_main_recovery_uses_frozen_proof_without_attestation_observation tests/test_integration_main_promotion.py::test_attestation_is_frozen_and_revalidated_before_prewrite`
  returned **2 passed, 3 warnings in 1.44s**.
- Before the final additions, the complete main-promotion file gate
  `pytest -q tests/test_integration_main_promotion.py -x` returned
  **29 passed, 3 warnings in 6.23s**. The later added focused nodes are included
  in the final 238-test affected-area gate below.

## Final verification

- The first affected-area gate exposed one test-only assertion mismatch after
  runtime behavior and public rebuild had succeeded:
  `aq test tests/test_integration_main_promotion.py tests/test_integration_candidates.py tests/test_integration_ci.py tests/test_integration_promotion.py tests/test_integration_contracts.py tests/test_integration_ownership.py tests/test_integration_repair.py -x`
  returned **1 failed, 205 passed, 11 warnings in 52.81s**. The regression treated
  `integration_repair_stages.current_subject` as a string, while the canonical
  repair API persists `{kind, revision, candidate_sha}` JSON.
- After changing only the assertion to that exact structured subject:
  `pytest -q tests/test_integration_main_promotion.py::test_current_moved_main_expires_then_public_rebuild_creates_next_revision`
  returned **1 passed, 3 warnings in 1.25s**.
- Final affected-area rerun, exact command above: aq slot 1/2, three workers;
  **238 passed, 11 warnings in 55.25s**.
- Changed-file Ruff:
  `ruff check src/integration/candidates.py src/integration/main_promotion.py src/commands/integration_commands.py tests/test_integration_main_promotion.py`
  returned `All checks passed!`.
- Compile:
  `python3.12 -m py_compile src/integration/candidates.py src/integration/main_promotion.py src/commands/integration_commands.py tests/test_integration_main_promotion.py`
  completed with exit 0 and no output.
- `python3.12 -m alembic heads` returned `d4a81f0c9e72 (head)`.
- `git diff --check` completed with exit 0 and no output.

## Files changed

- `src/integration/candidates.py`: explicit candidate mutation-purpose boundary
  and blocking treatment for live root-main claims.
- `src/integration/main_promotion.py`: typed attestation contract, immutable proof
  binding, prewrite/horizon enforcement, root-only applied recovery, moved-main
  supersession, and App-auth exact descendant import.
- `src/commands/integration_commands.py`: orchestrator-owned attestation resolver
  injection for the default root promotion service.
- `tests/test_integration_main_promotion.py`: crash/restart, concurrent prewrite,
  public rebuild, real-Git descendant, exact attestation, command, and post-main
  regressions.

## Self-review and concerns

- Hierarchy-first locking is preserved. Provider observation, installation-token
  acquisition, exact fetch, ancestry checks, and pushes occur outside database
  transactions. Every durable claim/prewrite/applied/superseded transition
  re-locks and validates the relevant immutable identity.
- No write can follow a pre-existing prewrite marker. No candidate-owned path can
  consume a root-main claim. A stale observed nonce/deadline cannot supersede a
  successor.
- The attestation resolver is server-injected, strict, exact, and fail closed.
  Direct command/service paths cannot replace it with caller prose or mappings.
  Recovery after main movement performs no attestation or CI observation.
- Fix 1b still must address the accepted root member/receipt structural binding,
  full frozen-member finalizer comparison, and SQLite receipt-trigger retention.
  Those files and behaviors were deliberately not modified here.
- `RootPromotionService` remains a large, single-purpose durable protocol module.
  Splitting it during this safety fix would risk obscuring transaction and replay
  boundaries, so no broad refactor was attempted.
- The 11 final-gate warnings are the existing `pkg_resources`, namespace-package,
  and `audioop` dependency deprecations. No local suppression or unrelated
  warning cleanup was added.

## Commits

- Runtime/tests: `464730aaaedd9ecbce32cc61ca3e21185a5f9b36`.
- Report: recorded by the following documentation commit.
