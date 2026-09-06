### Finding Verdict

- **The terminal publication transition is not fenced by the reservation execution nonce, allowing a stale pre-takeover owner to finalize over its successor.** — **ADDRESSED.** `_finish_publication` now rejects a reserved row whose current nonce differs from the caller's claim and repeats that exact nonce predicate in the affected-row `reserved -> published` update (`src/integration/attestation.py:493-521`). Therefore an expired unmarked takeover rotates the row nonce and permanently fences the former owner, while marked post-expiry reconciliation remains valid because marked claims never rotate their nonce.

### New Breakage in the Fix Diff

- None. The published-row replay branch remains immutable and verifies the exact check-run/external identity; the new fence applies only while the row is reserved.

### Test and Quality Evidence

- The public race test drives two separate `IntegrationAttestationService.publish()` instances through the exact ordering: old owner authenticates record `7000` and pauses, its unmarked lease expires, the successor takes over and installs its prewrite marker, the old finalizer loses, and the successor alone publishes/finalizes record `7001` (`tests/test_integration_attestation.py:629-707`). It asserts the old result is stale and proofless, one successor POST occurred, and the durable row holds the successor ID.
- The appended report records the focused RED on the prior code, GREEN on the corrected race, and `18 passed` for the complete attestation test file, plus clean Ruff, compilation, and diff checks (`task-10b-report.md:413-463`). I did not rerun those tests.

### Verdict

**Spec verdict:** PASS — 0 Critical, 0 Important.

**Quality verdict:** Approved. The read fence and write CAS now enforce the same exact nonce, and the behavior-based two-service regression covers the previously reachable stale-owner ordering without weakening marked ambiguity handling.

**Fix round:** All findings addressed, no new Critical/Important breakage.
