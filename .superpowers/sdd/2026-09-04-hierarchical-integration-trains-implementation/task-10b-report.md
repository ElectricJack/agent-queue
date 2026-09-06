# Task 10b implementation report

## Scope and revisions

- Reviewed Task 10a base: `71ee5db7`.
- Nearest preceding controller documentation head, preserved unchanged: `2e15d4f6`.
- Runtime/tests/workflow commit: `26c9e463` (`feat(integration): publish exact candidate attestations`).
- No schema or migration was required. No operator database, live GitHub credential,
  network, forge, branch-protection, project-enablement, push, or PR mutation was used.
- `aq prime` was invoked once at startup and reported that a task ID is required in this
  delegated shell. Per the delegation, it was not retried and no queue record was changed.

## Delivered contracts

### Exact candidate trust and provider

`src/integration/attestation.py` adds the frozen `AttestationPublicationResult` and
`IntegrationEnablementProbeResult` models plus `IntegrationAttestationService`.

- The service derives the canonical repository, numeric GitHub repository/full name,
  current root operation/stage, revision/head, required check policy, publication identity,
  and durable aggregate evidence under the established hierarchy-first lock.
- Provider and Git I/O occurs only after that transaction closes. The service obtains a
  repository-bound App token, imports the exact candidate OID into the daemon retained store,
  and reads only `<candidate_sha>:.github/agent-queue-integration.json`. It never reads a
  checkout/default-branch manifest or caller-supplied trust object.
- The candidate manifest must match the canonical repository ID, numeric/full-name binding,
  configured attestation App, distinct CI producer, frozen check version, and exact ordered
  names. Malformed, duplicate, missing, oversized, empty, mismatched, or inactive-style
  documents fail closed.
- Publication first reconstructs the complete authenticated aggregate from exact check-run,
  suite, workflow-run, and attempt identities and requires its canonical external ID to equal
  the durable aggregate. It then creates/readbacks the canonical Task 9a attestation.
- The newest exact-name record from the trusted App is authoritative. A newer invalid record
  blocks; there is no older-record fallback. Lost response and post-create crash replay reuse
  the single existing record.
- After every provider operation the exact hierarchy subject and aggregate are locked and
  revalidated before a proof is returned. `resolve` returns only the reviewed Task 9b2
  `RootAttestationProof` whose typed subject exactly equals the request.
- The enablement projection is read-only and reports missing App, repository mismatch,
  unresolved debug intelligence class, incompatible protection, and absent/failed scratch
  probe. Reader errors are blockers, not successes.

### Runtime wiring and zero-post-main boundary

`src/orchestrator/core.py` constructs repository-bound `GitHubAppClient` instances from the
closed daemon configuration and owner-only private-key provider. It injects
`handle_candidate_ci` into the single reviewed Task 10a `IntegrationService` and exposes
`IntegrationAttestationService.resolve` as the mandatory production resolver. Construction
performs no provider I/O.

`pending_candidate_ci_page` includes durable `green` candidates while their current batch is
still `testing`/`repairing`, allowing publication and lost-response replay. `promoting`,
`promoted`, cleanup-pending, old revision, and otherwise non-current rows remain excluded, so
restart reconciliation cannot perform CI observation or attestation after the main boundary.

### Hosted workflow and credential isolation

`scripts/check-integration-attestation.py` is a pure, standard-library, fail-closed decision.
It rejects duplicate/unknown/missing fields, bool-as-int/nonpositive record IDs,
noncanonical payload bytes, malformed identities, and any predicate mismatch. It selects the
numeric newest exact-name trusted-App record before validation, so an invalid newest record
cannot fall back.

`.github/workflows/tests.yml` now:

- checks out a commit-pinned action at the exact event SHA and verifies `git rev-parse HEAD`;
- performs a bounded, pinned-header, paged exact-name check-run read for main pushes;
- converts lookup/API/parse failure to `skip_full_ci=false` and uses `always()` so the safe
  full-CI route still runs;
- skips the three full jobs only for exact trusted main-push evidence;
- runs integration-branch pushes through the three full jobs exactly once while suppressing
  their matching integration PR event;
- preserves exact check names `Tests (default)`, `Tests (migration-and-slow)`, and
  `Tests (postgres-integration)`.

There is currently no authenticated declared-focused-check transport available to hosted CI
for ordinary task PRs. Those PRs therefore deliberately retain the safe full three-arm CI
fallback; this phase does not claim focused task-PR routing. The workflow receives only its
ordinary GitHub workflow token for readback and never receives a daemon App private key or
installation token.

The exact production App secrets are path-referenced/in-memory and are never sourced from the
daemon environment. As defense in depth, `src/sessions/env.py` prevents the reserved
`AQ_INTEGRATION_GITHUB_APP_*` namespace from being restored by a worker-controlled harness or
launch extension. The real `SessionSpecBuilder` regression proves sentinel key/token values are
absent from argv, env, prompt, serialized command data, and logs while an explicitly authorized
harness `GITHUB_TOKEN` retains the existing scrub behavior.

The exact trust-document path was intentionally not added to this repository. The existing
example remains documentation, and the feature remains fail-closed/inactive until Task 11
installs reviewed real identities and completes enablement probes.

## TDD evidence

### Candidate provider RED/GREEN

RED:

```text
$ pytest -q tests/test_integration_attestation.py::test_publish_reads_trust_from_authenticated_exact_candidate_oid
ERROR collecting tests/test_integration_attestation.py
ModuleNotFoundError: No module named 'src.integration.attestation'
1 error, 2 warnings in 0.35s
```

First exact-tree GREEN:

```text
$ pytest -q tests/test_integration_attestation.py::test_publish_reads_trust_from_authenticated_exact_candidate_oid -x
1 passed, 2 warnings in 0.55s
```

Crash/replay, newest-invalid, and probe boundary:

```text
$ pytest -q tests/test_integration_attestation.py::test_publish_crash_replays_existing_record_without_duplicate tests/test_integration_attestation.py::test_newest_invalid_trusted_record_blocks_older_success tests/test_integration_attestation.py::test_enablement_projection_is_read_only_and_fail_closed -x
3 passed, 2 warnings in 0.82s
```

Enablement reader failure RED:

```text
$ pytest -q tests/test_integration_attestation.py::test_enablement_projection_is_read_only_and_fail_closed -x
1 failed, 2 warnings in 0.63s
E OSError: local read failed
```

Enablement failure GREEN:

```text
$ pytest -q tests/test_integration_attestation.py::test_enablement_projection_is_read_only_and_fail_closed -x
1 passed, 2 warnings in 0.52s
```

Final provider-focused file, including exact trust failures, authenticated aggregation,
post-I/O stale revalidation, crash and lost-response replay, newest-invalid no-fallback,
enablement, and post-main selector exclusion:

```text
$ pytest -q tests/test_integration_attestation.py -x
12 passed, 2 warnings in 1.90s
```

### Workflow RED/GREEN

RED before the script existed:

```text
$ pytest -q tests/test_integration_workflow.py -x
1 failed, 2 warnings in 0.31s
python3.12: can't open file 'scripts/check-integration-attestation.py'
```

The first routing assertion then correctly exposed the missing explicit integration-push
predicate:

```text
$ pytest -q tests/test_integration_workflow.py -x
12 passed, 1 failed, 2 warnings in 0.91s
E assert 'refs/heads/aq/integration/' in workflow
```

GREEN after the explicit one-run routing and final fail-safe workflow refinements:

```text
$ pytest -q tests/test_integration_workflow.py -x
13 passed, 2 warnings in 0.85s
```

### Session credential isolation RED/GREEN

RED against explicit harness restoration:

```text
$ pytest -q tests/test_session_spec.py::TestEnvMarkers::test_integration_app_credentials_never_enter_real_session_spec -x
1 failed, 2 warnings in 0.29s
E assert 'PRIVATE-KEY-SENTINEL' not in serialized SessionSpec/log output
```

GREEN:

```text
$ pytest -q tests/test_session_spec.py::TestEnvMarkers::test_integration_app_credentials_never_enter_real_session_spec -x
1 passed, 2 warnings in 0.22s
```

### Wiring and selector compatibility

```text
$ pytest -q tests/test_orchestrator.py::test_orchestrator_owns_single_integration_service_loop -x
1 passed, 3 warnings in 1.62s

$ pytest -q tests/test_integration_service.py -x
12 passed, 2 warnings in 1.25s
```

## Final verification

Required affected-area gate:

```text
$ aq test tests/test_integration_ci.py tests/test_integration_attestation.py tests/test_integration_workflow.py tests/test_github_app.py tests/test_session_spec.py tests/test_integration_main_promotion.py -x
aq test: slot 1 of 2, -n 3
198 passed, 1 skipped, 11 warnings in 14.01s
```

Static verification:

```text
$ ruff check src/integration/attestation.py src/database/queries/integration_reconciliation_queries.py src/orchestrator/core.py src/sessions/env.py tests/test_integration_attestation.py tests/test_integration_workflow.py tests/test_orchestrator.py tests/test_session_spec.py scripts/check-integration-attestation.py
All checks passed!

$ python3.12 -m py_compile src/integration/attestation.py scripts/check-integration-attestation.py
[exit 0]

$ python3.12 - <<'PY'
from pathlib import Path
import yaml
value = yaml.safe_load(Path('.github/workflows/tests.yml').read_text())
assert isinstance(value, dict) and 'jobs' in value
print('workflow yaml parsed')
PY
workflow yaml parsed

$ git diff --check
[exit 0]
```

Warnings are the existing `pkg_resources`, namespace-package, and Discord `audioop`
deprecations; no warning originated in the Task 10b files.

## Self-review and exclusions

- Confirmed App/token/Git/check-run work is never performed while a database hierarchy
  transaction is open. Both publication and resolution re-lock and compare the exact snapshot
  afterward, closing the stale-subject window without holding locks over network latency.
- Confirmed the durable aggregate external ID is the canonical payload digest consumed by
  publication and readback; caller prose/dictionaries cannot manufacture a proof.
- Confirmed exact current operation, active stage, candidate revision/head, publication
  repository, ordered checks, producer, and green evidence are all required.
- Confirmed trusted record selection treats malformed ordering IDs and newer invalid records
  as terminal validation failure, never as permission to use older success.
- Confirmed workflow API failures result in full CI, and `always()` avoids a failed lightweight
  dependency suppressing the safe jobs.
- Confirmed no Task 10c route, release, cleanup, source comment, ref deletion, root-main
  protocol, live probe, protection mutation, or activation was added.

Concerns carried forward: Task 11 must provide real reviewed App/check identities, verify the
installed askpass/Git topology and protection/probe prerequisites, install the exact trust
document, and set the workflow App/version variables before enabling any project. Until then,
all missing configuration fails closed and main continues through full CI.
