# Daemon latency diagnosis and bounded observations

Task: `bright-impact-14`. This extends the fleet metrics and dashboard
performance specifications; dashboard process separation is a separate task.

The October 23 live sample shows event-loop stalls in historical session
hydration and repeated playbook command schema generation, alongside database
pool exhaustion. Optimize these without weakening current-session identity,
delegation provenance, lifetime native child counts, or contract drift checks.

The default flock read may omit terminal history only when it cannot influence
the output: retain current executions (including sleeping sessions), the selected
historical execution for each agent, unlinked executions of agents' current
tasks, creator sessions of active delegated tasks, and sessions with native
child events. Explicit `include_stopped` continues to return full history.

Feature compatibility checks must read feature metadata from the current
registration without generating argument/result schemas or fingerprints.
Full validation retains those projections and its contract drift checks.
Custom contract lookup implementations keep the existing `get` fallback.

Pool display excludes stopped sessions in SQL, preserving sleeping and
quarantined instances and the scheduler's complete inventory. Resuming sessions
are still added by their authoritative continuation identity.

Background scheduler sync, migration bridge, resume reservation and deadline work
have a 250 ms graph-fence acquisition budget, including the final backoff.
Busy reconcilers refuse with the existing retryable `wait.graph_busy` result;
they must not pin the pool for 20 seconds while the holder needs another
connection. Foreground writes retain their existing 20-second budget. Mail
acknowledgements and deadline writers replace unbounded SQL waits with the same
bounded fence. Fence ordering, transaction ownership, and retries remain
unchanged. Resume preparation and launch keep the foreground budget, because
their failure path can place a continuation on hold. Resource-only or empty
proposed edges cannot introduce a semantic cycle and skip graph loading;
proposals containing semantic edges retain the complete cycle check.

Extend `perf.operations` with bounded operation labels, latency histograms,
outcome counters, and in-flight counts for git subprocesses, tmux calls,
orchestrator cycles, and wait-graph lock acquisition. Existing recursive metric
rollups merge histograms and counters. Timing includes failures and cancellation;
instrumentation cannot change operation results. Slow observations produce JSONL
fields accessible through `aq logs`, without argv, SQL text, parameters, terminal
contents, credentials, repository URLs, or session IDs as metric labels.
The existing `metrics.perf_enabled` switch disables the observations.

Asyncio debug/slow-callback detection remains opt-in through Python's
`PYTHONASYNCIODEBUG=1`; the low-overhead loop drift probe and bounded watchdog
remain the default. Do not globally enable expensive debug stack capture on a
busy fleet to diagnose its latency.

Validation must compare complete flock results against the full-history input,
exercise live feature metadata and custom lookups, verify cancellation and
metric rollups, and record before/after timings. Live rollout and daemon restart
belong to the operator. Local measurements must be identified separately from
post-deployment observations.

## October 23 evidence

The live baseline was collected with
`aq logs -F -n 100000 --since 30m --json`, from 07:17:47.940250Z through
07:47:47.153210Z: 8,561 records, retained locally at
`/tmp/bright-impact-14-baseline-full.jsonl`. These are the existing daemon's
logs, before this branch was deployed. No operator database or configuration
was modified. The box was serving a normal active worker fleet; its observed
load averages were approximately 9, 12, 14 and daemon CPU was 85%.

| Signal | Observed baseline | Interpretation and change |
| --- | --- | --- |
| Agent roster | 85 slow requests; median 2,082 ms, maximum 5,466 ms | Repeated watchdog stacks enter session hydration through the flock command. Select only history that affects the response. |
| Pool status | 110 slow requests; median 11,360 ms, maximum 26,516 ms | Watchdog samples include pool-session hydration. The display path no longer converts stopped history or looks up its task statuses. |
| Event loop | 44 watchdog records, including paired overdue/recovery records | Session hydration and playbook schema generation recur in sampled stacks. This is not 44 independent stalls or a CPU profile. |
| Database | 483 pool waits and 1,661 query durations at least 1 second | Pool-wait logs show all 20 connections checked out and 30-second pool timeouts. Query duration includes scheduling and lock delay; it is not server execution time alone. |
| Wait fence | Seven `wait.graph_busy` records in this window, with the old 20-second wait | Code inspection identifies nested connection demand by graph holders. Short background contenders release pool slots; the two-connection regression exercises that mechanism. |
| Git contention | Two daemon git children each near one CPU during the process snapshot | This supports contention, but the snapshot did not capture argv. Fsck and claim Git-budget remediation belongs to PR 1797 (`quick-quest-81`), not this branch. |

The route medians above include **only logged slow requests**, not all calls.
For comparison, 18 slow health requests had a median 1,351 ms and maximum
2,570 ms, suggesting shared daemon delay rather than only endpoint-local work.
Do not treat `/api/execute`'s 440 slow observations as interactive latency:
that route also carries intentional claim/message long polls.

The pool/fence feedback loop is an inference from the measured pool exhaustion
and inspected call paths, not a live PostgreSQL lock-holder trace. Scheduler
sync holds the graph fence while `AdmissionReader` uses additional pooled
connections. Twenty-second contenders can occupy those connections. The fix
preserves atomic graph changes and the existing retryable refusal; it does not
pretend that a busy lock was acquired. Scheduler transactions remain bounded by
their existing timeout. Further subdivision of scheduler work should be driven
by post-deployment operation timing if long holders persist.

## Local before/after checks

Ten warm runs of `unsupported_features` across all 17 shipped
`src/prompts/reviewed_playbooks/*/artifact.json` definitions took a median
214.6 ms through the old full-schema lookup and 16.0 ms through the feature-only
lookup. Ranges were 181.6–298.3 ms and 13.8–20.0 ms respectively, with identical
results. Definitions were loaded before timing; this measures compatibility
checks, not total artifact loading or live request latency. Reproduce by wrapping
`RegistryContractLookup.get` in `SimpleNamespace(get=lookup.get)` for the old
fallback and passing the lookup itself for the new path.

The flock regression seeds 2,005 session rows and selects six while comparing
the complete command response against the original full-history input. It keeps
historical delegation parents, native starts, sleeping sessions, current-task
preference and legacy unlinked executions. Run it serially with `-s` to print
query-plus-hydration timings; no machine-speed assertion is used. One serial
run on the shared test host measured 123.43 ms for the full read and 29.36 ms
for the selective read. This is a single local sample, not a fleet latency
percentile; the durable regression assertion is response equivalence.

The graph regression uses an isolated two-connection pool. A holder takes the
fence, a contender occupies the second slot, and the holder requests another
connection. The contender must return the retryable refusal and release its
connection so the holder can finish; the fence is reacquirable after commit.
Existing semantic-cycle tests still exercise the full traversal.
The resume regression holds the fence during dispatch and verifies the
continuation remains queued, with no workspace preparation or failure hold.

## Validation record

Tests used the disposable PostgreSQL service on port 5534 with
`POSTGRES_TEST_DSN`; worker database scope and refusal sentinels were preserved.
The end-to-end kit uses its own home, port 8219, tmux socket and database
(`aq_e2e_bright_impact_14`), separate from the operator daemon.

- Initial flock, feature and metrics checks: 125 passed.
- Wait graph, pool reconciliation and API metrics checks: 158 passed.
- Final resume contention and common-capacity regressions: 2 passed.
- Serial flock equivalence/measurement check: 1 passed.
- The 23-file area run covered metrics, API middleware/clients, flock/session
  queries, playbook validation, Git/tmux, wait admission/dispatch/hibernation and
  the orchestrator: 1,199 passed. Two catalogue checks raced the last resume
  test's import change and its regeneration; they required a focused rerun
  after generation completed. That final catalogue run passed all 56 checks.
  No product test failed in the area run.
- `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh`: all 20 scenarios passed,
  including claim races, scope fencing, provider failover/recovery and train
  publication. The wrapper stopped its disposable daemon after completion.
- Both API clients regenerated with `--offline` / `--from-file`; selection
  catalogue regenerated. Changed Python paths pass Ruff and `git diff --check`.

## Operator rollout and comparison

Ownership boundaries confirmed by supervisor message `msg-bcf985fd`: leave
`quick-quest-81`/PR 1797, `sharp-ember-93`'s hibernation transition budget,
`keen-forge-77`'s escalation work and `bright-cascade-28`'s dashboard process
topology unchanged. The supervisor requested branch validation and this live
measurement procedure, with no worker-managed deployment or wait for approval.

After the normal deployment, collect another 30-minute window under comparable
fleet load. Record the deployment SHA, active workers, pool size, host CPU/load,
and whether PR 1797 and dashboard separation are deployed. Export the same log
window and `GET /api/metrics/series?from=<epoch>&to=<epoch>&step=1m`.
Compare these metrics rather than extrapolating local microbenchmarks:

1. Merge route histograms for `POST /api/agent/list`, `POST /api/pool/status`
   and `GET /api/health`; compare counts, p50/p95 estimates and maximums.
   Keep long polls separate. Histogram percentiles are bucket estimates.
2. Compare `perf.loop.drift`, watchdog stacks, `perf.db.pool_wait`, query
   histograms, checked-out connections and pool timeouts. Graph-busy refusals
   may initially increase because refusal is faster; verify eventual wait
   delivery and absence of repeated scheduler timeout/starvation.
3. Inspect `perf.operations` for `git.<verb>`, `session.tmux`,
   `provider.tmux.start/stop/nudge`, `orchestrator.cycle` and
   `db.wait_graph_lock`. Histograms and outcomes merge across samples;
   `in_flight` and `peak_in_flight` are per-sample gauges (coarser tiers average
   them, so use 1-second samples when checking exact concurrency peaks).
4. Slow API and DB logs now carry numeric `duration_ms` fields. Operation logs
   at one second or longer carry `operation`, `duration_ms`, `outcome` and the
   surrounding command/request correlation. Durations include expected waits
   and cleanup; `completed` means returned, not domain-specific success.
5. If delays remain, use the new attribution to target the surviving holder or
   operation. In particular, report persistent fsck pressure to PR 1797's owner
   and endpoint polling/process concerns to the dashboard-separation owner.

No post-deployment improvement is claimed by this report. Production rollout
and a comparable fleet-load window remain operator follow-through.
