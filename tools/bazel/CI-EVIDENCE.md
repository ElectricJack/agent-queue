# Nonrequired shadow evidence, version 1

`bazel-shadow.yml` pairs every dev push, PR merge SHA and manual run with the
reusable `bazel-legacy-sample.yml`. Both use `ubuntu-latest` and explicitly check
out the same `github.sha`. No required Tests check, train policy, attestation or
suite promotion changes. Dev runs have no superseding cancellation group.

Each legacy matrix entry runs one existing obligation with the explicit outcome
recorder. The Bazel job resolves dependencies with lockfile error mode, then runs
`bazel test --config=shadow //checks:all` four times:

| Phase | Output/server state | Disk action cache |
| --- | --- | --- |
| incremental | fresh hosted runner | restored compatible prior snapshot |
| cold | separate fresh root | separate empty cache |
| warm | unchanged cold root | cold invocation's cache |
| server_discard | `clean --expunge` of cold root | cold invocation's cache retained |

Remote execution/cache are disabled. State, disk caches and reporting outputs
live outside the checkout. Invocation metadata never enters cached result bytes.
Linux namespace admission is explicit; unsupported isolation fails the job.
The existing test launcher enforces private process/network/filesystem isolation.

The job summary and `sample.json` contain each phase's executed/eligible target
and action fractions, cache sources, wall seconds and five slowest targets
(maximum shard duration). `actionsExecuted` includes internal and disk-hit
actions; it is not an uncached-process count. The denominator is the configured
BEP action graph, with an explicitly named same-graph cold fallback if warm
analysis omits it. No `actionsCreated` estimate is substituted. Cold/warm time
covers the Bazel test command; dependency resolution and output-discard commands
have separate timing receipts. Changes to common lock/toolchain/launcher inputs
are named separately from source/graph edits. Compare actual incremental
fractions with dependency-closure evidence; a category never waives broad fanout.

The report validates BEP graph completeness, target/shard coverage, successful
finishes and the existing strict parity comparator. Each backend's context must
also match the reporting checkout. Warm and discarded-server runs require every
test result cached and unchanged raw sidecar/JUnit/lifecycle digests. Missing,
cancelled, retried, failing or incomplete samples cannot pass. Flaky/no-cache
targets therefore prevent the unchanged-cache condition. Dashboard comparisons
retain independent artifact manifests as well as command outcomes.

## Retention and dev counters

Artifacts `legacy-*` and `bazel` retain raw JUnit, outcome/lifecycle sidecars,
BEP, profiles, lock copies, plan/graph digests and timing/log receipts.
`bazel-shadow-evidence-v1` retains `sample.json`, `summary.md`, `window.json`
and history retrieval diagnostics. All request 90-day retention. Before starting
an evaluation, verify repository retention permits that window; export all raw
artifacts and reports before expiry if the evaluation extends past it. A report
without its original receipts is insufficient evidence for cutover.

The history collector reads the last 100 first-parent commits through the exact
dev SHA, scans up to 300 push runs and joins reports by run ID, attempt and SHA.
Each suite counts consecutive complete, protected-dev samples. Missing commits
(including intermediate commits never pushed separately), missing artifacts,
identity/outcome differences and ambiguous duplicate samples reset that suite.
The current report explicitly records retrieval failures and scan bounds.
Counters are observational; reaching twenty does not authorize promotion.
For a longer retained evaluation, provide the complete oldest-first first-parent
commit list and downloaded report directories:

```bash
python scripts/bazel/ci_sample.py window --commits commits.json \
  --samples /absolute/retained-reports --output /absolute/window.json
```

Keep one report per commit; duplicate attempts require an explicit documented
operator choice. Keep the full failed/missing history as well. Synthetic fixtures
are regression coverage and cannot supply real longitudinal samples.

## Cache trust and cost

Keys bind version, trust scope, OS, architecture, Bazel and toolchain/dependency
locks, with immutable commit save suffixes and compatible restore prefixes.
Protected dev writers require a protected push event. PR writers use a separate
PR-number namespace and GitHub's merge-ref cache scope. Protected jobs restore
only their protected namespace. Dispatch jobs use an untrusted read namespace
and never save. No secrets are supplied to test commands. API tokens are used
only by reporting steps and stripped on cross-host artifact redirects.

`cache.json` records transfer wall times, step outcomes, matched keys, disk size,
archive sizes when available, current repository usage and the provider's current
storage-limit response. There is no assumed fixed quota. API denial remains
explicitly unknown. Historical inventories with the same namespace and complete
pagination bounds expose disappearing entries and fractions; deletion and
provider eviction cannot be distinguished, so causal eviction rate remains
unknown. Repository usage/limit observations and transfer cost inform a later
cache decision; they never change storage settings or buy infrastructure.

Provider interface: [GitHub cache REST API](https://docs.github.com/en/rest/actions/cache?apiVersion=2026-03-10).
BEP interface: [Bazel 8.3.1 protocol](https://github.com/bazelbuild/bazel/blob/8.3.1/src/main/java/com/google/devtools/build/lib/buildeventstream/proto/build_event_stream.proto).
