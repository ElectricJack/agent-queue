# Task 11 requirements

Task7b safety cutover prerequisite: managed repair writer commands require a server-issued
session-instance-bound API token. Legacy NULL-bound tokens retain unrelated behavior but
cannot author/push a conflict resolution or file under an active repair scope. Explain fresh
token mint/session restart at cutover; preserve local/elevated remint authorization. Do not
enable operator projects or mint real credentials during implementation. Downgrading7b
requires draining/reconciling live resolution reservations before DDL; never discard pending
remote-mutation evidence to force rollback.

CI/probe rulings: read ci-auth-preflight.md plus Task9 trust/config contract. Implement
GitHub.com enablement blockers; no claim of GHES support. Credentials are operator-provided
daemon-only file/provider references, never defaults. Probe target must be an explicitly
configured operator-preprovisioned scratch repository, not one AQ creates automatically;
require the same app/effective protection and a separate configured non-bypass negative
control identity. Without a matching positive+negative probe receipt, train enablement
is blocked. Probe identity includes host/app/install/repo/protection digest/software and
attestation schema; invalidate on relevant change. Never run an actual probe or change
real protections during this implementation: test with fake HTTP/Git adapters and document
the explicit operator action. Retain scratch probe commits for audit and delete only its
exact expected candidate ref. Keep all secret bytes out of status/config serialization.
Task9a credential transport additionally requires enablement to prove the installed packaged
askpass helper and every containing directory are outside worker write authority. Verify the
resolved helper's regular-file ownership/mode/content identity and the trusted root-owned Git
HTTPS remote-helper layout; an unsafe or unsupported installation is a stable blocker and must
never fall back to argv-only trust or a pre-populated credential FD.

Task6 introduces projects.hierarchical_integration_policy nullable JSON with typed
parent/root policy inputs (required_checks, repair, branchless_parent, on_failed_child)
and explicit compiled-artifact bindings. Reuse this configuration seam in authenticated
controls, validate complete policy/producer/debug/artifact identities before enablement,
and preserve already-frozen operation snapshots when project defaults change.
Validate configured primary and required branchless-verifier routes from frozen boundary
policy fields as well as debug routing. Debug is explicitly operator-designated higher
intelligence; do not invent a numeric/lexical class ranking absent from the registry.

Legacy merge cutover includes the final-reviewer path: its shipped legacy profile says
merge before closing. Enabled projects must route final approval without ordinary
pr_merge, and the canonical managed-project merge command must reject that bypass
unless it is the exact integration promotion protocol. Disabling only a timer/sweep is
insufficient. Preserve legacy behavior for disabled projects; test enabled final-reviewer
approval cannot merge a root directly to main or a child outside its parent fence.

Existing projects.integration_mode is direct|pull_request and must retain that meaning. The new
disabled|observe|hierarchy|train rollout mode is orthogonal; persist it separately with project
policy/artifact configuration rather than overloading integration_mode. Add a focused migration
if earlier tasks have not already established this field. Tasks 5/8 need a durable shared feature
gate seam before this CLI lands; coordinate through the controller.

## Global Constraints

- Only one root integration lease is active per project; every batch names one designated target repository.
- Every non-empty sweep uses an ephemeral integration branch, including a single root PR.
- Every eligible root at the snapshot is included; there is no batch cap, ejection, bisection, or speculative next train.
- Children branch from and deliver to their immediate parent. Parents verify the delivered aggregate before completion.
- Batch membership never changes after sealing.
- `main` advances only from the expected base SHA to the exact full-CI-tested candidate SHA.
- No redundant full-CI audit after ordinary promotion to `main`.
- One primary repair stage, one higher-intelligence debug stage, then a human; duration and attempt limits are configurable at both root and parent boundaries.
- Playbooks own policy; deterministic core contracts enforce identity, ownership, idempotency, and delivery evidence.
- Use `aq test` beyond a single test file. Never increase worker counts or run the whole suite during implementation. Ruff only on changed Python files.
- Never migrate the operator database, change worker DB environment variables, or run `aq start` from a worker slot. Generate migrations against scratch infrastructure and exercise them only in test databases.
- Read both repository instructions and this design before executing a task. Changes remain flag-disabled until the operator performs the documented cutover.


## 3. Shared types and protocol

All IDs are strings, timestamps UTC epoch seconds, Git OIDs validated hexadecimal strings, and repository identity is the configured canonical repository ID rather than a checkout path or arbitrary remote alias. Workspace kinds resolve to this ID before authorization. This release integrates one designated repository per project; roots with code in another repository are visibly ineligible with `repository_not_designated`, not silently omitted from an otherwise eligible snapshot.

Define these immutable Pydantic value types in Task 1:

```python
class BranchKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repository_id: str
    branch: str

class Fence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: BranchKey
    owner_id: str
    token: int

class PromotionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation_key: str
    source_task_id: str
    source_head: str
    source_base: str
    expected_target: str
    fence: Fence

class PromotionValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_id: str
    receipt_id: str | None = None
    prepared_sha: str | None = None

class RequiredCheckSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    names: tuple[str, ...]
    producer_id: str

class RepairPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    primary_seconds: int = Field(default=1800, gt=0)
    primary_attempts: int = Field(default=3, gt=0)
    debug_seconds: int = Field(default=3600, gt=0)
    debug_attempts: int = Field(default=3, gt=0)
    debug_intelligence_class: str
    debug_profile_id: str | None = None
```

The names above are new implementation interfaces, not claims about existing classes. Import `BaseModel`, `ConfigDict`, and `Field` from Pydantic. Preserve repository ref validation in GitManager. Reject an empty required-check set for code-producing integration. Resolve the configured debug intelligence class before enabling a project; do not assume a profile named Fable exists.

Mutation results use the existing `CommandResult` envelope with typed values and named outcomes from design §10.2. Known validation failures return those outcomes; unexpected I/O errors remain retryable/runtime failures with durable operation identity. Never convert an unknown Git result into successful delivery.


## 15. Task 11 — Operator status, controls, and project cutover

**Files:** Create `src/integration/status.py`, `tests/test_integration_controls.py`, `docs/guides/hierarchical-integration-trains.md`. Extend integration handler/contracts and the existing project configuration/CLI command transport. Add `src/cli/integration.py` and register it in `src/cli/app.py` after reading `src/cli/CLAUDE.md`.

**Interfaces:** `integration_status(project_id)` returns schedule, batch/revision, members, parent blockers, ownership, budgets, evidence, and cleanup; `integration_flush(project_id)`, `integration_resume(operation_id)`, `integration_abort(operation_id, reason)`, `integration_retry_cleanup(batch_id)`, and `integration_enable(project_id, mode)` use the existing authenticated command envelope. Modes are `disabled`, `observe`, `hierarchy`, `train`.

- [ ] Test worker/reviewer/integrator/human capabilities and cross-project argument rejection. Assert only a human can abort/resume a human-blocked operation and abort never rewrites main.
- [ ] Run `aq test tests/test_integration_controls.py -x`.
- [ ] Implement read projections with stable blocker codes: open child, missing receipt, stale head/generation/review, wrong repository, active owner, pending CI, budget exhausted, human hold, cleanup conflict. `aq integration status --project-id <id>` and `aq task explain` expose these reasons without requiring log inspection.
- [ ] Implement observe mode as read-only eligibility/reporting; it does not reserve refs, write receipts, or start repair agents. Enable preflight verifies designated repository, origin retention, required checks, debug routing, integrator authority, and branch protection compatibility.
- [ ] Require explicit historical gate waiver with recorded operator identity/reason; do not fabricate pinned-review receipts. Disable the migrated project's legacy merge sweep and child merge gates as part of the atomic configuration transition. Existing unreviewed/unpinned work stays held until reviewed or waived through the documented migration procedure.
- [ ] Document inspection, flush, human repair/resume, abort, cleanup retry, and rollback. Disabling future schedules never abandons an active train. Rollback waits for active operations to finish/abort and restores legacy policy explicitly, without deleting audit records or downgrading the DB.
- [ ] Run focused controls tests and commit. If implementation adds public API DTOs or a codegen router rather than using existing generic execution, regenerate both API clients with the repository scripts and run `tests/test_api_client_contract.py`; never hand-edit generated clients.

Example status payload contract:

```json
{"project_id":"p","mode":"train","batch_id":"b1","revision":2,
 "state":"awaiting_ci","pending_sweep":true,
 "repair":{"stage":0,"attempts":1,"deadline_at":1800},
 "blockers":[{"code":"pending_ci","head_sha":"recorded-candidate-oid"}],
 "cleanup_pending":[]}
```

## Prescriptive preflight sequence

1. Keep `projects.integration_mode` unchanged (`direct|pull_request|NULL`) and
   reuse `hierarchical_integration_mode` as the effective rollout mode. Add
   separate desired/draining state plus append-only cutover, explicit-waiver,
   and scratch-probe records. Probe receipts bind project, canonical and numeric
   production/scratch repository identities, host, positive and negative App/
   installation identities, protection/trust/transport digests, schema, software,
   and protocol versions. Fingerprint changes make receipts stale; never mutate
   receipts in place.
2. Make scratch-probe config a closed non-secret mapping of IDs, exact owner/repo,
   and private-key file references. Reject inline key/PEM/token/body/auth and
   unknown fields at load, editor, schema, and get-config boundaries without
   echoing input. Integration credential config is restart-required unless every
   client/token cache is demonstrably rebuilt atomically; prefer restart-required.
3. Expose one public read-only installed transport inspector shared with Task9a
   runtime pinning. Verify `/usr/bin/git`, exec-path, HTTPS helper symlink/final
   inode, interpreter, `/proc`/credential-passing facilities, packaged askpass
   path/owner/mode/digest, and every containing directory. Separately evaluate all
   worker execution postures/roots/profiles. Same-UID unconfined, bypass-capable,
   editable, or otherwise writable installations block with
   `askpass_worker_writable`; file mode/digest alone cannot pass. Apply equivalent
   path-authority checks to private-key paths. Freeze the accepted fingerprint and
   compare it before every credentialed train mutation.
4. Add typed GitHub.com-only protection inspection using narrow repository-bound
   provider reads. Verify App/install/repository identity and permissions; combine
   classic branch protection and active rulesets into canonical facts/digest.
   Require the exact numeric integration App bypass/update allowance, no broad
   untrusted bypass, and a repository-capable negative identity outside the
   allowance. Hidden/ambiguous/unreadable rules are `protection_unverifiable`.
   Scratch and production relevant protection digests must match.
5. `integration_probe` is an explicit local-operator command; `enable(train)`
   never runs it implicitly. The configured scratch repository is preprovisioned.
   Persist the run before mutation, then with fakeable adapters: verify identities/
   protection/transport; make one isolated probe commit; exact-create candidate
   ref; publish/read canonical attestation; require a repository-capable negative
   identity's exact-main push rejection and unchanged main; require the positive
   App's identical expected-old push and exact readback; exact-delete only the
   candidate ref; retain commit; persist positive+negative receipt. Crash replay
   reconciles the same run. No live probe occurs in implementation/tests.
6. `integration_status` is DB/read-only projection with stable sorted blockers for
   rollout, schedule, members/readiness, ownership, repair budgets/human hold, CI,
   promotion/reconciliation, cleanup, trust/transport/protection/probe health.
   Extend task explain with the same vocabulary. Observe mode may do provider
   reads but writes no schedules/leases/refs/intents/receipts/evidence/checks/
   repair tasks/gates; flush returns eligibility only.
7. Before hierarchy/train, resolve the complete typed parent/root policy, required
   and branchless verifier compiled artifacts, profiles, debug class/profile,
   check set/trust manifest, designated GitHub.com repo, retained object source,
   provider identities, runtime/playbook readiness, transport/protection, and
   matching probe receipt. Observe may retain visible blockers. Do not invent a
   numeric/lexical intelligence ranking.
8. Cutover performs external reads first, then hierarchy-locks and revalidates all
   fingerprints before one CAS that changes effective mode/schedule, disables the
   project legacy sweep/gates, and appends audit/waiver. Guard ordinary `pr_merge`
   before forge/CI for hierarchy/train; preserve disabled/observe behavior. Disable
   with active work sets desired disabled and drains frozen operations before
   restoring the recorded legacy policy. Rollback is forward configuration, never
   history deletion or DB downgrade.
9. Register status/flush/resume/abort/retry-cleanup/enable/probe through the generic
   command envelope and CLI client. Human controls are local-operator only; status
   is same-project; server resolves all relationships; abort only handles human-
   blocked state and never rewinds/pushes main. CLI reads `src/cli/CLAUDE.md`, uses
   `emit()`, and never opens DB/key files locally.
10. Add read-only doctor checks and the operator guide. Document operator-only
    `aq db current`/`aq db upgrade`, restart, observe, explicit probe, hierarchy then
    train, inspection/flush/human controls/cleanup/drain rollback. Doctor never
    offers automatic fixes for credentials, protection, probes, or migrations.

Required focused tests include config closure/redaction/reload; installed topology
and worker-write authority; key path safety; App/trust/protection binding; probe
success/negative/crash/cleanup/fingerprint invalidation; observe non-mutation;
status/explain; authority/cross-project controls; atomic cutover/waivers/legacy
merge bypass/draining; doctor/CLI/session cutover; and worker-scope DB refusal.

## Resolved implementation choices

- Keep `integration.github_app` as the positive identity. Add closed non-secret
  `integration.scratch_probe` configuration naming the scratch numeric repository
  ID/full name and a distinct negative App/installation with private-key reference.
  Reuse the existing positive identity and configuration redaction conventions.
- Effective mode remains `projects.hierarchical_integration_mode`; desired mode
  and drain state are separate, as required above. Transition/waiver/probe records
  are append-only, with one new migration for the complete phase.
- Add local-operator-only `integration_waive_history(project_id, reason,
  blocker_digest)`, returning a durable waiver ID. Enablement consumes only a
  matching current blocker-set waiver; identity comes from authenticated context.
- Status allows same-project scoped reads. Enable/probe/waive/resume/abort/cleanup
  retry require trusted local operator authority. Flush uses existing authenticated
  project authorization: disabled returns disabled; observe and hierarchy report
  eligibility without mutation; train creates/coalesces a manual schedule request.
- Suppress legacy merge routing per project, never disable the global pipeline.
  Preserve historical `pr-merged` gates; waiver changes their applicability for
  migration without resolving/deleting them or manufacturing reviewed receipts.
- Share one public installed-transport inspector with runtime, taking explicit
  worker-write-authority facts from server configuration. Reuse existing topology
  and key validators internally; do not build parallel inspections.
- Consume reviewed Task10 provider/blocker seams; implement only missing durable
  probe/control facts here. Build status/persistence/config first within this phase,
  then probe/cutover/CLI, followed by the independent phase review.
- Drain/abort/resume must include reviewed Task10b unresolved attestation publication
  reservations alongside existing Git mutation claims. Expiry is reconciliation
  authority, not permission to discard an ambiguous provider write.
- Train preflight also verifies the hosted-workflow App/version variables introduced
  by Task10b match the exact trust document and configured identities. Missing or
  unreadable variables remain a visible blocker because they force redundant full
  main CI. Document operator setup; enablement does not silently write forge
  variables or install fabricated identities. Ordinary task PRs currently retain
  full-CI fallback; state this limitation accurately in the guide.
