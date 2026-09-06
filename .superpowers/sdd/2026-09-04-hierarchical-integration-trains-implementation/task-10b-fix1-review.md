### Finding Verdicts

1. **A fork PR can suppress its only full-CI run by choosing the reserved-looking branch prefix.** — **ADDRESSED.** Duplicate-PR suppression now requires a pull-request event, exact equality between the head and workflow repository, and the actual deterministic `aq/integration/p-<32 lowercase hex>/r-<32 lowercase hex>` ref shape (`scripts/check-integration-attestation.py:193-218`). The workflow consumes that separate decision without weakening main attestation fallback (`.github/workflows/tests.yml:84-120`), and the behavioral test covers same-repository, fork, and malformed same-repository refs (`tests/test_integration_workflow.py:218-225`).

2. **The hosted decision rejects valid required checks that share one GitHub Actions check suite.** — **ADDRESSED.** The verifier now enforces unique check-run IDs while comparing the set of check suite IDs to the unique workflow-attempt suite set (`scripts/check-integration-attestation.py:91-142`). The focused test constructs two distinct required checks in one suite and obtains the trusted decision (`tests/test_integration_workflow.py:116-151`).

3. **Concurrent fresh publishers can create more than one canonical attestation, including after promotion has begun.** — **NOT ADDRESSED (Important).** The durable reservation closes the immediate two-fresh-service race and gates main, rebuild, and stage invalidation, but its final transition is not fenced by the reservation execution nonce. Expired unmarked takeover changes the nonce with an exact CAS (`src/integration/attestation.py:427-450`); `_finish_publication` then selects and updates any row that is merely `reserved`, without requiring `row.execution_nonce == claim.execution_nonce` in either the read validation or final update (`src/integration/attestation.py:477-517`). A former owner that authenticated a provider record before its lease expired can therefore resume after takeover and finalize over the successor, even after the successor has installed its prewrite marker. If the provider observations differ across that boundary (for example, the successor observes a newer record), the stale owner can freeze the immutable row to the older ID and make subsequent resolution fail permanently; if the successor already marked an empty observation, its already-authorized POST can also create the duplicate the reservation was intended to prevent. The new tests cover immediate concurrency and a takeover only after the old process has crashed (`tests/test_integration_attestation.py:587-659`), not a live old writer resuming across takeover. **Minimal remediation:** require the final reserved-to-published CAS to match the claim's exact execution nonce (plus the existing identity/state), and test a paused old owner, expired unmarked CAS takeover, successor prewrite, then old-owner resume; the old final CAS must lose and only the canonical successor may finish/post. Marked post-expiry reconciliation still works because marked claims never rotate their nonce.

4. **Production root promotion is constructed without the configured GitHub App client.** — **ADDRESSED.** Core retains the daemon-created repository-bound factory (`src/orchestrator/core.py:1479-1501`), the command constructor passes it to root promotion (`src/commands/integration_commands.py:284-300`), and root promotion derives the requested binding from the frozen server-side attestation and rejects a client with any different binding (`src/integration/main_promotion.py:371-411`). The production-shaped command test proves the exact `99/acme/widgets` binding reaches a real promote call, while missing configuration remains push-free and blocked (`tests/test_integration_main_promotion.py:730-824`).

5. **Proof construction can attach a malformed record's ID to a different strictly validated payload.** — **ADDRESSED.** `select_trusted_attestation` now strictly validates App and record IDs and returns one immutable `SelectedAttestation` containing both the selected ID and its payload (`src/integration/ci.py:144-201`); proof construction consumes those two fields from that one result (`src/integration/attestation.py:748-766`). Provider read, required-check observation, publication reuse, and hosted decision tests include bool/float identities and fail closed (`tests/test_integration_ci.py:164-188`, `tests/test_integration_ci.py:251-277`, `tests/test_integration_ci.py:332-360`, `tests/test_integration_workflow.py:166-181`).

### New Breakage in the Fix Diff

- **None separate from finding 3.** The missing final nonce fence is an incomplete correction of the original exclusive-publication finding, not an unrelated expansion.

### Out-of-Scope Observations

- The known dependency warnings and environment-gated PostgreSQL skip are carried exactly as directed and are not reopened in this fix round.

### Verification Evidence

- The appended report records the amended affected-area gate as `316 passed, 1 skipped, 11 known warnings`, SQLite and PostgreSQL migration cycles, changed-file Ruff, compilation, migration-head, YAML parse, and diff checks (`task-10b-report.md:333-389`). I did not rerun those suites.
- Focused inspection confirmed provider/Git I/O remains outside the hierarchy transactions, main blocks every unresolved reserved publication, marked claims remain non-takeover reconciliation-only, and rebuild/stage invalidation block live or marked claims (`src/integration/attestation.py:90-159`, `src/integration/main_promotion.py:169-197`, `src/integration/candidates.py:338-376`, `src/integration/repair.py:799-823`).

### Verdict

**Spec verdict:** FAIL — 0 Critical, 1 Important open.

**Quality verdict:** Needs fixes. Four original findings are fully addressed, but exclusive publication is not yet a complete fenced state machine because a stale pre-takeover writer can win the terminal transition.

**Fix round:** Findings remain open — finding 3 requires an exact execution-nonce CAS at finalization plus the live-old-writer takeover regression test.
