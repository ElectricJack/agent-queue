# Task 10b — Candidate-tree trust, exact attestation, and workflow isolation

This is the second independently reviewed Task 10 phase. It begins only after Task 10a passes
review and after Task 9b2 fix 1a/fix 1b are independently approved. It owns the production
attestation provider and workflow decision boundary. It does not own root playbook routes,
release, cleanup, or operator activation.

## Dependency gate and reviewed seams

Consume, without redesigning:

- Task 9a `GitHubAppClient`, `GitHubRepositoryBinding`, isolated HTTP/token broker, exact-ref and
  exact-OID transport, and `AuthenticatedGitHubObserver` conventions;
- Task 9b1's exact repository-bound candidate publication identity;
- Task 9b2's reviewed `RootAttestationSubject`, `RootAttestationProof`, and mandatory
  `AttestationResolver` pre-main gate;
- Task 10a's bounded `pending_candidate_ci_page`, `IntegrationService` handler injection, and
  durable outbox/restart behavior.

If the reviewed Task 9b2 signatures differ, update this brief before implementation; do not add a
parallel proof type or weaken promotion to accept an untrusted boolean. Task 10c waits for 10b's
provider and workflow contract to pass review.

## Outcome and file ownership

Create:

- `src/integration/attestation.py` — candidate-tree trust loading, canonical publication/readback,
  exact proof resolver, and read-only enablement probe results.
- `scripts/check-integration-attestation.py` — fail-closed pure workflow decision CLI.
- `.github/agent-queue-integration.json` only if the reviewed inactive example is promoted to the
  exact trust-document path; it must remain explicitly inactive and contain no fabricated live
  IDs. Preserve `.github/agent-queue-integration.example.json` as documentation if still useful.
- `tests/test_integration_attestation.py` — provider/publication/replay/tree-trust tests.
- `tests/test_integration_workflow.py` — script truth table and workflow routing assertions.

Modify only as required:

- `src/integration/ci.py` — reuse `IntegrationTrustManifest`, `AttestationPayload`,
  `select_trusted_attestation`, `CandidateCISubject`, and `CIService`; add no legacy rollup path.
- `src/integration/service.py` — install the candidate-CI handler from 10a; do not change scheduler
  or outbox authority.
- `src/integration/main_promotion.py` — production wiring only if the reviewed Task 9b2 resolver
  injection needs a concrete adapter; do not change root-main mutation semantics.
- `src/git/github_app.py` — only repository-bound check-run reads/writes that use the existing App
  broker and strict repository identity.
- `.github/workflows/tests.yml` — lightweight main attestation decision and exact event routing
  while retaining the existing full matrix.
- `src/env_scrub.py`, `src/sessions/env.py`, or `src/sessions/spec.py` only if the mandatory real
  `SessionSpecBuilder` sentinel test exposes leakage.
- Existing `tests/test_integration_ci.py`, `tests/test_github_app.py`, and
  `tests/test_session_spec.py` for compatibility assertions owned by those modules.

Do not add schema unless a focused RED proves the reviewed Task 9/10a durable evidence cannot
represent publication replay. If schema is truly required, stop and report the missing exact
identity before creating a migration.

## Frozen trust and provider contract

The source of truth is the exact candidate tree's `.github/agent-queue-integration.json`, schema
`aq.integration-trust.v1`. It binds canonical `RepoConfig.id`, numeric GitHub `repository_id`,
`full_name`, distinct numeric `ci_producer_app_id` and `attestation_app_id`, exact name
`Agent Queue Integration Attestation`, and a versioned nonempty ordered required-check set.
Load those bytes by authenticated exact-OID access from the candidate commit, never from the daemon
checkout, workspace, PR label, commit message, status context, caller dictionary, or mutable
default branch.

Production construction must prove the configured App and `GitHubRepositoryBinding` match both
canonical and numeric identities. The newest exact-name record created by the trusted attestation
App is authoritative. Missing, malformed, unavailable, wrong-App, wrong-repository, wrong-head,
wrong-version, incomplete, skipped/neutral, duplicate, or newer invalid trusted records fail
closed; never fall back to an older success. CI producer identity stays distinct from the
attestation producer.

Define in `src/integration/attestation.py`:

```python
class AttestationPublicationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    outcome: Literal["published", "already_published", "not_green", "stale", "configuration_blocked"]
    subject: RootAttestationSubject
    proof: RootAttestationProof | None = None

class IntegrationEnablementProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    ready: bool
    blockers: tuple[str, ...]

class IntegrationAttestationService:
    async def publish(self, subject: RootAttestationSubject) -> AttestationPublicationResult: ...
    async def resolve(self, subject: RootAttestationSubject) -> RootAttestationProof | None: ...
    async def enablement_blockers(self, canonical_repository_id: str) -> IntegrationEnablementProbeResult: ...
```

`publish` first loads the exact candidate-tree manifest and revalidates the durable exact-current
green aggregate. Provider/token/Git I/O occurs outside a DB transaction. It creates the canonical
`aq.integration-attestation.v1` `output.text` with full required check/run/suite/attempt identities
and `external_id = aq-attestation-v1:<sha256 of canonical bytes>`, then authenticated-readbacks the
record. Reacquire hierarchy-first authority and exact subject/head/evidence before persisting or
returning the proof. A crash after provider creation replays by newest trusted exact-name lookup;
it must not publish a duplicate. `resolve` performs the same exact authenticated read and returns
only a proof whose subject equals the requested `RootAttestationSubject` byte-for-byte.

Publication happens only after durable exact-current trusted green and before Task 9b2's main
prewrite/push. No CI observation or attestation publication occurs after main. Git ref
reconciliation is not a CI audit. Missing production App/forge dependencies return
`configuration_blocked` or `None`, never a success stub.

The probe is read-only in this phase: it reports blockers for missing trusted integration App,
repository mismatch, unresolved debug intelligence class, incompatible branch protection, or
absence/failure of the separately recorded positive-and-negative scratch probe. It neither runs a
live probe nor enables a project; Task 11 owns those operator mutations.

## Workflow decision and routing

The script's decision must reduce exactly to:

```python
skip_full_ci = (
    event_name == "push" and ref == "refs/heads/main"
    and attestation.producer_id == configured_integration_app_id
    and attestation.repository_id == repository_id
    and attestation.head_sha == checkout_sha
    and attestation.required_check_version == required_check_version
    and attestation.conclusion == "success"
)
```

The workflow selects the newest trusted exact-name check before constructing `attestation` and
invokes the script with already-authenticated record data. Invalid JSON/duplicates/types or any
lookup/API failure yields a successful lightweight decision step with `skip_full_ci=false`; the
full jobs then run. Hosted CI never contacts the operator database and receives no daemon App
private key or installation token.

Modify `.github/workflows/tests.yml` so:

- every main push performs the lightweight decision first and skips full CI only for the exact
  predicate above;
- integration-branch pushes run full CI exactly once, while a matching integration PR event does
  not duplicate that full run;
- ordinary PRs use authenticated declared focused checks only when that transport exists and is
  exact; otherwise they retain the safe full-CI fallback;
- checkout is pinned and verified against the event/attested candidate SHA before any skip;
- the existing check names remain exactly `Tests (default)`, `Tests (migration-and-slow)`, and
  `Tests (postgres-integration)`, including SQLite migration and PostgreSQL coverage.

Do not collapse the three jobs into different check names or accept GitHub's skipped/neutral
conclusion as green.

## TDD slices and acceptance

1. RED/GREEN exact candidate-tree trust loading: wrong tree/default-branch file, malformed bytes,
   inactive/missing manifest, canonical/numeric repository mismatch, same CI/attestation App, and
   empty/skewed checks all fail closed.
2. RED/GREEN exact CI aggregation and publication: multiple workflows, attempts and suites; wrong
   producer/head/version; partial/skipped/neutral/newer-invalid evidence; only exact success
   publishes. Assert publication occurs before the first Task 9b2 main mutation call.
3. RED/GREEN crash/replay: crash after check creation before local return, fresh service/provider
   memory loss, and lost response all resolve one canonical proof with no duplicate. Stale
   candidate/revision/evidence after provider I/O cannot satisfy promotion.
4. RED/GREEN workflow truth table for push/PR/ref, missing and malformed manifests/checks, wrong
   App/repository/SHA/version/conclusion, newest-invalid record, checkout mismatch, integration
   branch push versus matching PR, ordinary PR fallback, and all three exact matrix check names.
5. RED/GREEN zero-post-main behavior: promoted/cleanup-pending rows never enter candidate-CI
   selectors and neither `CIService.observe_candidate` nor attestation publication is called by
   restart/reconciliation.
6. RED/GREEN real `SessionSpecBuilder` construction with sentinel private-key/token values in the
   daemon environment plus explicit harness environment. Assert both secrets are absent from
   argv, env, prompt, serialized command data, and captured logs; authorized session markers and
   harness credentials still follow existing scrub rules.
7. RED/GREEN enablement blocker projection using fake App/protection/probe readers only. No live
   credentials, forge write, protection edit, or project enablement.

Required final gate:

```bash
aq test tests/test_integration_ci.py tests/test_integration_attestation.py \
  tests/test_integration_workflow.py tests/test_github_app.py \
  tests/test_session_spec.py tests/test_integration_main_promotion.py -x
```

Run the script's direct unit/fixture tests, changed-Python Ruff, YAML/static workflow validation,
and `git diff --check`. Record exact RED/GREEN/final evidence, files, commits, self-review, and the
confirmed absence of live calls in `task-10b-report.md`; commit runtime/tests/workflow first, then
the report. No whole suite or worker-count increase.

## Binding exclusions

No root playbook or command contracts, release/catch-up conversion, cleanup rows/actions, source
PR comments, ref deletion, main push redesign, live forge/probe/credential use, repository
protection mutation, operator activation, Task 11 controls, Task 12 E2E, daemon start, push, or PR.
Keep the feature disabled.
