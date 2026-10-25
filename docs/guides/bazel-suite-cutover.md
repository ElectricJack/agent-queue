# Preparing one Bazel suite switch

The committed `.github/agent-queue-suite-cutover.json` has no active suites.
Its legacy names/version exactly match `.github/agent-queue-integration.json`;
all legacy Tests jobs remain required and dispatchable. V3 shadow evidence stays
nonrequired. This preparation does not authorize a cutover or establish parity.
The six operational rollout tasks own activation in this order:

| Suite selector | Prepared required checks | Bazel obligations |
| --- | --- | --- |
| `dashboard` | `Bazel (dashboard)` | `//checks:dashboard` |
| `cli` | `Bazel (cli)`, `Bazel (cli_startup)` | `//checks:cli`, `//checks:cli_startup` |
| `default` | `Bazel (default)` | `//checks:default` |
| `postgres` | `Bazel (postgres)` | `//checks:postgres` |
| `migration_slow` | `Bazel (migration_slow)`, `Bazel (generated)` | `//checks:migration_slow`, `//checks:generated` |
| `e2e` | `Bazel (e2e)` | `//checks:e2e` |

The CLI startup budget remains covered by `Tests (cli-conformance)` until the
CLI switch, then by its own explicit Bazel check. Generated-artifact validation
remains covered by `Tests (migration-and-slow)` until the migration/slow switch,
then by `Bazel (generated)`. Neither obligation can be dropped from the catalog.

## Prepare and verify

Dispatch **Tests** on the exact candidate ref with the `prepare_suite` input set
to one selector. The workflow runs that suite's complete Bazel aliases alongside
all legacy jobs; this input does not change any required checks or trust version.
The job verifies its checkout SHA, requires Linux namespace isolation, installs
checksum-locked Bazel and retains its build event file for 90 days. There is no
remote execution or shared cache write. The V3 paired shadow workflow continues
to supply parity/cache/budget evidence separately.

Create a reviewable draft from the current approved catalog and trust manifest:

```bash
python src/integration/suite_cutover.py plan --suite dashboard \
  --sha EXACT_40_CHARACTER_CANDIDATE_SHA --output /tmp/dashboard-switch.json
```

The draft contains the proposed catalog, trust manifest, complete `RequiredChecks`
policy input, audit Actions variable and required check names for protection
review. It is always marked `activated: false` and `verified: false`. Names,
alias targets and the legacy version determine the new check-set version.
Partial catalog/manifest updates are refused. Preparation allows only the next
ordered activation; rollback can restore any one active suite independently.

Stage the catalog and trust changes together in the reviewed candidate. Re-run
the candidate checks after every commit, including the configuration commit:
evidence for an earlier SHA cannot authorize the new SHA. Keep the installed
train policy requiring the legacy checks during preparation; all legacy jobs
still run, so that policy remains satisfiable.

Train candidate reporting executes the verifier from its trusted default-branch
revision and treats candidate JSON only as data. It permits at most one suite
transition, preserves trusted repository/App identities and obligation mappings,
and checks every required name's newest trusted run on the exact candidate.
A pending or failed rerun supersedes earlier green. Deploy the preparation to
the trusted default branch before attempting any operational switch; an old
verifier or missing input must fail closed. Candidate reporting first resets its
status to pending, and records failure for failed Tests runs or verification
errors, so an earlier successful status cannot authorize a rerun.

For an operator's preflight, retain the GitHub check-run pages and verify the
proposed files against the currently trusted files:

```bash
python src/integration/suite_cutover.py candidate \
  --catalog PROPOSED_CATALOG --trust PROPOSED_TRUST_MANIFEST \
  --trusted-catalog CURRENT_CATALOG --trusted-trust CURRENT_TRUST_MANIFEST \
  --sha EXACT_40_CHARACTER_CANDIDATE_SHA --runs CHECK_RUN_PAGES_JSON
```

`CHECK_RUN_PAGES_JSON` is the array of pages from `gh api --paginate --slurp`
for that repository's `commits/SHA/check-runs?filter=all&per_page=100` endpoint.
The verifier prints the version only after the complete set succeeds. This
does not replace the daemon's authenticated provider read. The corresponding
daemon input is `RequiredChecks.from_suite_catalog(...)`; `require_candidate`
re-evaluates `ExactChecks` evidence against that entire proposed policy and head,
rather than trusting a cached aggregate green flag. `train.py:exact_gate` retains
its existing exact-head check inside the publication fence.

## Operator activation and receipts

Every suite needs 20 consecutive exact-dev parity samples, 100% unchanged-result
cache reuse, all budgets met, no unresolved flaky quarantine or graph
under-declaration, an auditable obligation manifest and the approved review's
sign-off. Follow the [accepted testing strategy](testing-strategy.md#bazel-acceptance-and-delivery-evidence)
for the evidence contract. Code tests and a candidate green do not satisfy these
operational gates.

Treat activation as one controlled trust rotation. Use the existing train
playbook/batch controls to hold publication while applying the reviewed inputs.
Do not resume until all of these agree and their receipts name the same exact
candidate and suite version:

1. The candidate's catalog, Tests workflow aliases and committed trust manifest.
2. The complete required names/version/producer in the reviewed installed policy
   and future frozen train inputs. Change project policy through
   `aq project set ... integration-policy ... --expected-integration-generation ...`;
   existing in-flight snapshots remain frozen and need explicit disposition.
3. `AQ_INTEGRATION_REQUIRED_CHECK_VERSION` and the attestation App variable,
   configured through the operator's `aq integration app-setup` path.
4. Applicable branch protections, reviewed by the repository administrator with
   CI checks pinned to the trusted producer and attestation pinned to its App.
5. Fresh authenticated exact-candidate check evidence, `app-verify`, manifest
   comparison and the approval/parity/measurement receipts.

The [train onboarding runbook](../config/train-onboarding.md#3-app-credential-mode)
defines the supported commands and trust anchors. No helper here edits operator
policy, branch rules, variables, installed playbooks or a database. A failure or
partially applied rotation keeps publication held. Hosted verification binds
the payload's complete names/version to the pushed manifest and re-reads the
attested live runs. Missing, forged, stale or mismatched configured attestations
use the existing full legacy Tests fallback. Unconfigured installations retain
the existing compatibility behavior. Explicit full-legacy promotion-source audit
checks remain a separate supported check set; they cannot satisfy ordinary
candidate attestation.

## Rollback and coexistence

Generate a single-suite rollback with the same `plan` command plus `--rollback`,
using the current approved files and exact rollback candidate SHA. It restores
that suite's legacy required names and derives the matching version for the
remaining active suites. It restores the original legacy version when none
remain. Apply the same held-publication rotation, verification and receipt steps;
preserve the original evidence. Re-activation needs the relevant parity window
again and respects the rollout prerequisites.

Keep every legacy arm dispatchable for at least two weeks after its switch.
For a week after each switch compare p50/p90 push-to-green (queue and runtime),
landing latency, worker wait, compute, rebases/reruns and red-dev rates against
the recorded baseline, segmented by suite and change size. Require at least
20 comparable landings; insufficient observations extend the hold. Roll back
immediately if push-to-green does not improve, rebases/reruns rise or missing
verification causes red dev. Capacity/queue regressions count as regressions.
Retirement requires the two-week window, successful comparison, rollback rehearsal
and operator acknowledgment in the separate R2 work. Pytest/npm developer paths,
worker smart selection and optional testmon experiments remain available.
