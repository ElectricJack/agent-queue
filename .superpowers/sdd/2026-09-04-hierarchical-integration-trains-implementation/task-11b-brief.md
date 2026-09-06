# Task11b requirements — transport/protection/explicit scratch probe

Read this first, task-11-brief.md for binding exact constraints, task-11-slices.md
for ownership/worker-authority rulings, task-11a-report.md for reviewed config/query
interfaces, task-11b-provider-notes.md and ci-auth-preflight.md for provider contracts.
Do not read the whole implementation plan. Begin only after11a review approval.

## Scope and authority

Own shared installed-transport inspection/runtime enforcement, server-observed worker
write authority, typed effective protection/hosted-variable facts, replayable probe
and focused probe persistence migration.11c owns public commands/full cutover;11d CLI/
doctor/guide. No live GitHub calls, key access, scratch probe, sandbox provisioning,
operator config/DB mutation or activation in implementation/tests. Fake external
adapters, clocks and authority observations; real domain/persistence logic.

## Required outputs

1. One public installed transport inspector in src/git/askpass_broker.py reused by
GitManager runtime, not parallel pinning implementations. Cover exact executable,
exec-path, Git HTTPS helper symlink/final inode and ancestors, interpreter, packaged
askpass, /proc/credential-passing facilities, modes/ownership/digests and private-key
paths. Use existing pinned topology helpers, no argv-only trust or prepopulated FD.
Collect all routable worker postures from actual profile/harness/launcher composition,
including dormant profiles, bypass args and reachable workspace variants. Worktrees,
approval flags and cgroups are not filesystem confinement proof. No raw argv/env or
secrets in receipts/status. Same-UID unconfined or unproven/bypass/writable postures
block (askpass_worker_writable/worker_confinement_unverifiable). A typed injectable
inspector verifies real launcher/kernel policy if available; the default recognizes
current unsupported/unconfined topology and fails closed. Do not invent a sandbox
adapter or YAML claims. Fingerprint complete canonical posture/protected-path facts.

2. Accepted fingerprint must be frozen by the eventual cutover/probe consumers and
revalidated before every credentialed integration mutation. Provide an explicit
runtime seam usable by11c, with defaults that never claim safe enablement without
evidence; preserve current reviewed exact-App transport constraints. Profile/harness/
PATH/topology/policy/key authority changes invalidate proof. Do not leak private data.

3. Add narrow repository-bound reads in src/git/github_app.py and typed facts in new
src/integration/protection.py. GitHub.com only, exact numeric App/install/repository
identity and requested permissions. Combine classic protection and all effective
active inherited rulesets; hidden/incomplete/ambiguous facts are
protection_unverifiable. Require trusted numeric positive-App direct-update allowance,
no broad untrusted bypass and separately capable negative identity outside allowance.
Cross-resolve classic slugs or refuse. Compare security-relevant production/scratch
facts separately from addressing IDs while binding all IDs in final receipts. Read
hosted-workflow App/version variables and compare exact trust config; missing/unreadable
is blocked, never silently write variables. Reuse authenticated App client and shared
trust parser. No GHES support claim, no ambient gh credentials or SDK addition.

4. Implement IntegrationProbeService in src/integration/probe.py with public typed
run/replay and current-receipt validation interface; replace boolean/tuple proof seams
behind attestation.enablement_blockers with bound typed proof as needed. Explicit
operator command will call this in11c; enable(train) must not invoke it implicitly.
Use only configured preprovisioned scratch repo with matching relevant protection,
positive integration.github_app and distinct negative scratch_probe identity from11a.

5. Persist immutable run subject/fingerprints before I/O, durable bounded executor
claims/prewrite evidence for irreversible boundaries and append-only terminal probe
receipts. Own one focused migration after reviewed11a, conn-owned queries as appropriate.
Replay a pending run instead of starting a second ambiguous run. Bind project, internal/
numeric production and scratch repo IDs/full names, host, both App/install identities,
trust/protection/transport digests, software/protocol/attestation schema versions.
Relevant changes invalidate current receipt; never rewrite old proof. Partial downgrade
must refuse unresolved writes before dropping evidence. Both dialects tested.

6. Protocol: isolated probe commit; exact candidate-ref creation; observe/publish/read
canonical authenticated attestation for exact head; negative identity's SAME exact
expected-old main push must receive conclusive protection rejection and authenticated
unchanged-main readback; positive App then pushes identical candidate with identical
expected-old and exact readback; exact-delete candidate ref only, retain audit commit.
Do not fabricate CI success or treat network/credential failure as negative proof.
Pending real checks wait on the durable run; canonical shared observer/parser supplies
evidence. Lost responses reconcile exact persisted subjects, never blindly retry
irreversible publications. No real project batches/leases/receipts may be fabricated
to make scratch proof pass. Failures remain visible with no false successful receipt.

## Acceptance and handoff

Focused TDD for installed topology/worker authority/key safety; App/protection/trust/
hosted-variable binding; probe positive/negative/failure/crash/replay/cleanup/fingerprint
invalidation; dual-dialect schema/live-downgrade guards; secret sentinels absent from
logs/argv/env/config/status/session specs. New test_integration_protection.py and
test_integration_probe.py plus precisely affected existing Git/App/attestation tests.
Do not run giant Git test files unfiltered during iteration. One final explicit
affected-area gate only, aq test no upward workers/full suite; Ruff changed Python.

Read repo instructions, use apply_patch, exact owned-path commits, no subagents.
Write full RED/GREEN commands/output, deliverable self-review, interfaces/persistence
contract, default-deployment limitations and concerns to task-11b-report.md; return
short DONE/concerns contract. Raise concrete blockers promptly rather than inventing
new security architecture. Root can execute exact unique-scratch PostgreSQL nodes.
