# Task10b fix1 — exact production attestation boundary

Read task-10b-review.md completely. Its Critical and four Important findings
are the binding correction requirements; address them as one phase.

1. Restrict duplicate-PR suppression to same-repository integration publications
   using the actual generated ref convention. A fork using that prefix must run
   full CI. Test the actual decision, not mere workflow substring presence.
2. Accept multiple distinct required check runs in the same Actions suite, while
   retaining unique check IDs/names and exact unique-suite/workflow coverage.
3. Introduce the missing durable exclusive publication reservation. A new focused
   migration/table is authorized: the review demonstrates that external_id is not
   a provider idempotency key. Bind it to exact project/batch/revision/head/evidence/
   payload identity; use hierarchy-first short transactions, an exclusive execution
   nonce, bounded I/O, and an immutable pre-POST ambiguity marker. An expired marked
   attempt may only reconcile authenticated provider state, never blindly POST
   again. Unmarked expiry may use affected-row CAS takeover. Main cannot cross an
   unresolved publication, and revision/stage invalidation must not grant authority
   around an unresolved publisher. Do not hold DB locks across provider calls or
   create another Git ref-mutation protocol. Reuse existing patterns/helpers.
   Verified publication keeps one exact canonical proof. Test two fresh services,
   crash/lost response, and publication versus main in both orderings through the
   public services. Any unresolved ambiguity remains visible/retryable or blocked.
4. Wire a real repository-bound App factory into the actual root command service
   construction. Server resolves the repository from the requested batch/project;
   no caller-selected arbitrary binding. Missing configuration remains blocked.
   Test the actual command construction path, not only the attestation factory.
5. Return record ID and payload from one strict selector. Reject malformed bool/
   float App and record IDs without splicing metadata from another record. Keep
   newest-invalid behavior identical for provider and hosted verifier.

Scope includes the original Task10b files plus the necessary publication schema,
query/claim, invalidation/main gates, and focused regression files. Do not implement
Task10c/11 or introduce a separate orchestrator. The ordinary-PR full-CI fallback
remains explicit. Dependency warnings are documented; no warning suppression or
unrelated package upgrades are requested by the nonblocking Minor.

Use focused tests for workflow, attestation, real command wiring, currentness and
publication races. Test the new migration on scratch SQLite and a unique database
on disposable PostgreSQL port16833 only. Final gate covers amended integration
modules once; no whole suite/worker increase. Commit code/tests/migration, append
task-10b-report.md with exact commands/results/commits and concerns. No subagents,
new workspaces, live credentials/forge/operator writes, push or PR.
