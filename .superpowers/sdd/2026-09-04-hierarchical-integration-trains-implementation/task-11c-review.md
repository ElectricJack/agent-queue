# Task11c scoped review — 77585b64

Reviewer /root/review_operational_11c; Spec FAIL, quality Needs fixes. Critical0, Important6; inherited deprecation warnings Minor. No tests or runtime mutations by reviewer.

## Important findings (verbatim)

1. **Active train → observe bypasses draining.** `src/integration/controls.py:427` protects active work only when the requested mode is `disabled`. Requesting `observe` immediately changes effective mode and clears legacy suppression while frozen operations continue. Ordinary `pr_merge` consequently becomes available during that work. Require draining or reject transitions from managed modes to observe until frozen work finishes.

2. **Valid non-verifier policies cannot enable.** `src/integration/controls.py:302` inserts `None` into required profile/class tuples whenever `branchless_parent != "verifier"`, then rejects any `None` at lines 314–324. Both supported `skip` and `declared` policies therefore receive permanent configuration blockers. Include verifier entries only when required; add focused coverage for both alternatives.

3. **Operational results violate their registered contracts.** `src/integration/controls.py:438` returns `disabled` after a successful immediate disable, but `src/commands/contracts/integration.py:496` and `:1618` exclude that outcome. Separately, recovery returns string blockers (`src/integration/recovery_controls.py:181`, `:358`), whereas the shared value requires dictionaries (`src/commands/contracts/integration.py:114`). The existing adapter converts both into `contract_violation` (`:1440`). The shared value also omits detailed status fields such as schedule, active batch, repair, and cleanup, silently discarding them. Align service outcomes and typed values, and test real results through the generic adapters.

4. **Functional preflight remains incomplete.** `src/integration/controls.py:266` validates stored artifact metadata, while `:916` checks dependency presence, repository binding, and profile/class membership. Neither verifies loadable artifact content, required-check/producer configuration against the repository’s current trust manifest, or required hosted-workflow variables. Thus enablement can succeed with unavailable artifacts or CI configuration that later fails during attestation. These functional checks remain required by the override; implement read-only validation without restoring deferred security certification.

5. **History-waiver applicability is recorded but never consumed.** `src/integration/controls.py:332` includes every open historical merge gate, even after `:495` records that gate as inapplicable. A waived hierarchy cutover therefore still reports the gate blocked, and a subsequent train cutover demands another waiver. Reapplying the same gate conflicts with the applicability table’s project/gate primary key (`src/database/tables.py:3245`). Consult the immutable applicability evidence when determining current blockers, while retaining the historical gate itself.

6. **Durable destination suppression uses stale cached state after cutover.** `src/playbooks/runtime.py:103` snapshots suppression only during `refresh()`, and `:301` uses that snapshot when selecting destinations. The enable transaction does not refresh or invalidate it; acceptance directly selects from cached state (`:206`). A runtime initialized before enablement can therefore admit the legacy merge-sweep destination after successful cutover. Read authoritative suppression at acceptance or provide reliable generation-based invalidation; test enablement against an already-running runtime.

## Cannot verify requiring controller resolution

Actual resumed-event → repair-writer execution is not demonstrated by the new recovery test, which verifies database transitions using an operation without an established repair delegate: `tests/test_integration_operational_controls.py:483`. Verify the bounded operational path after fixes. Live provider readiness remains unverified; certification/matrix/backend-copy work remains explicitly deferred.

## Strengths

Cutover CAS/audit/waiver/suppression/schedule are transactional; authoritative scheduler and sealing/release guards respect draining. Narrow pre-forge pr_merge and exact final-review suppression preserve per-task evidence path. Certification is honest not_performed; cleanup retry preserves irreversible markers.

## Fix-round controller scope

Fix all six with focused tests only. For #1 use explicit blocked transition to observe while active (smallest approved safe alternative); user can disable/drain then observe. Preserve allowed inactive transitions. #4 is functional configuration/artifact readiness only, not deferred security/protection certification. Resolve resumed-event execution with an existing delegate fixture and real dispatch seam; do not add broad recovery matrix.
