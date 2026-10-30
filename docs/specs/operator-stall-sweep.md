# Operator stall sweep

`scripts/operator-checks/stall-sweep.py` is a read-only diagnostic for active
projects. `--project PID` confines task, holder and wait observations to that
project. Every daemon read uses the same envelope, authorization and completeness
checks. An authorization denial stops the sweep; failed or partial evidence never
proves either an orphan or a healthy queue.

Both `aq doctor --check stall.sweep` and the unscoped operator script report
every non-ready playbook activation as `playbook_activation_unhealthy` and
`PLAYBOOK-NOT-READY`, respectively, even with no queued tasks or pending events.
Health is recomputed against live command and profile contracts on each sweep.
Findings include playbook and scope identity, enabled state, artifact hash,
health reasons naming offending commands/profiles, and elapsed time since
activation (unknown when no activation timestamp exists). This age is not a
claim about when drift began. Recompiling and reactivating through the existing
reviewed path clears the finding once health is ready. The sweep never refreshes
fingerprints, recompiles policy, or activates automatically: a changed contract
does not by itself prove backward compatibility. Failed health reads remain
incomplete evidence. The script skips this system-only read in `--project`
patrols rather than widening their authority; the system doctor sweep covers all
activation scopes, including paused projects.

Paused train-mode projects are also checked through control-only integration
status. A `project_paused` blocker produces a `PAUSED-PROJECT` line, including in
scoped patrols where project enumeration is unavailable. The blocker names
completed live-origin sources still awaiting delivery or delivery proof. Cached
Git evidence suppresses sources already proven satisfied; unavailable evidence is
reported as unknown, never as a confirmed delivery backlog. The diagnostic does
not resume projects or change target discovery, which visits ACTIVE projects only.

For stale IN_PROGRESS worker tasks, read fresh `task show` claim identity and
match its session, agent, task and project together. Containers do not require
workers. A complete inventory with no holder or a stopped holder can report an
orphan. Conflicting identities report uncertainty instead of recovery advice.
A live session with fresh activity remains live regardless of agent heartbeat
age or harness. A sleeping holder is identified as an intentional hold.

For inactive holders, bounded project wait history locates possible waits;
`wait show` performs the canonical `blocking_wait_for` check of the current
instance, claim, deadline and producer. Only its positive retention guidance,
matching identity and a finite recent `checked_at` support WAITING-WORKER.
Historical, expired, cancelled, mismatched or unverified waits grant no exemption.
Incomplete history or validity reads report unknown evidence. The script does
not implement producer polling or claim policy and never stops/restarts workers.

Pool READY counts and task observations occur at different times. Compare scoped
project counters with scoped stale-task counts, not fleet totals. A count gap is
an aggregate discrepancy and cannot identify excluded task IDs. Existing task
explanations retain explicit dependency, gate and hold reasons; capacity-only
explanations do not establish exclusion.

Stale READY tasks whose `route_source` is `unrouted` receive an
`UNROUTED-READY` diagnostic when their canonical explanation confirms a routing
wait or failure. It includes the router's explanation even when a moving
dependency also explains the wait. This uses the existing 20-minute READY
threshold and scoped reads; it excludes containers through canonical routing
explanations. The sweep only reports the observation. Explicit re-runs request
routing immediately as specified in [task re-routing recovery](task-reroute-recovery.md).

Stale READY or DEFINED children whose canonical explanation includes
`frontier_epic_refresh_pending` receive a distinct epic-refresh-needed finding,
including the owning epic, blocking refresh batch ids when present, and
`aq integration refresh-epic --task EPIC_ID --apply`. Doctor's `stall.sweep`
reports the same condition as `epic_refresh_needed`. Use the explanation's
`blocking_task_ids` or the child's parent identity, never the generic frontier
reason's child `ref`, to name the epic. Missing epic identity remains explicit
uncertainty. These observations reuse task explanations without fetching Git or
changing admission. Refresh apply advances the existing immutable refresh batch;
a merge conflict uses the ordinary bounded train repair path, and the child
remains withheld until checks, attestation and publication finish. The diagnostic
does not authorize bypassing holds or open human gates.

## Patrol runtime

All subprocess reads share a monotonic 55-second sweep budget so the patrol's
60-second caller timeout leaves room for reporting and process cleanup. The
`--timeout` option accepts a smaller positive budget. Exhaustion prints findings
already observed plus `SWEEP-INCOMPLETE`, exits 1, and kills the current subprocess
groups (including shell pipelines). It never treats unobserved checks as healthy.
Authorization denial still exits 2 and stops the sweep without widening scope.

Independent reads run in waves of at most eight workers: project task lists,
queued-task explanations and paused/terminal child counts, referenced blockers,
verifier parents, and current worker claim identities. Only rows whose diagnosis
uses an explanation request one. Each identical observation, including a failed
read, is cached for this invocation; many dependents sharing a blocker need only
one completion read. Scoped patrols do not prefetch foreign-project blockers.

Explanations are read in batches: one `aq task explain --task-ids '<json list>'`
per project, at most 100 tasks per batch (the daemon's bound); a project with a
single candidate keeps the per-task read. The daemon answers explains one at a
time, about a second each under load, and most of that cost is the admission and
pool observation that a batch shares across one project's tasks. Before batching,
per-task explains of one busy project used up the whole 55-second budget. Each
batch entry seeds the per-task cache, so findings are unchanged. An entry that
failed is re-read on its own and reports exactly as a lone read would. A CLI or
daemon without `--task-ids` gets one silent probe, then the sweep falls back to
per-task reads for the rest of the run. A foreign task in a batch refuses the
whole batch the way it refuses a lone explain, so authorization still produces
one `SWEEP-AUTH` line and exits 2.
`--skip-pools` preserves the installed patrol's option to leave pool checks to a
global supervisor. `--timings` writes total elapsed time and per-command call
counts/cumulative subprocess time to stderr; concurrent durations overlap.

The focused suite verifies call counts, caching, scope and deadline cleanup with
isolated fake commands. Its opt-in timing check models 128 stale tasks with one
second of CLI/daemon latency per read and requires a complete sweep under 60s; a
second models daemon-serialized explains (0.5s each, a batch costing 46% per task,
as benchmarked) for 64 queued tasks and requires under 40s, where per-task reads
exhaust the budget:
`AQ_PERF_STRICT=1 aq test -m perf -p no:xdist tests/test_supervisor_stall_sweep_script.py`.
This synthetic workload is repeatable evidence of batching, not a live-daemon
latency measurement. Use `--timings` from the authorized patrol session to locate
any remaining endpoint or local-command delays.

## Auto-deploy stalls

`scripts/operator-checks/aq-auto-deploy.sh` is the canonical copy of the cron job
that fast-forwards the daemon's checkout to `origin/dev` every 5 minutes. It
refuses a dirty tree, a branch other than dev and a non-fast-forward. A refusal
used to be one log line, so on 2026-10-14 uncommitted edits in the live checkout
skipped every run for about 17 hours, unnoticed. The script now counts consecutive
skips in `~/.agent-queue/aq-auto-deploy.skips`. After six runs (30 minutes) it
messages `session:supervisor-agent-queue` once per stall, naming the reason, the
dirty files and how far `origin/dev` is ahead. A failed send retries on the next
run. A deploy, or a clean tree with nothing to deploy, resets the count.

The development checks report `AUTO-DEPLOY-STALLED` when the checkout is behind
`origin/dev` and either the log's trailing run of skips is older than 30 minutes or
the tree is dirty with no recent skip to explain it. A deploy or rollback line
ends a run, and so does a gap of more than 15 minutes between skips. The remedy
saves the edits on a rescue branch and in `~/.agent-queue/backups/`, then restores
a clean tree on dev. It never discards them. Agents never edit the live checkout:
they work in a worktree.

## Operator reconciliation

Branch publication does not update `~/.agent-queue/operator-checks/stall-sweep.py`.
The installed helper inspected for this change has different auth handling,
lacks the canonical preparation-refusal check, and adds a direct `agent list`
heartbeat-only orphan check with stop/restart advice. Preserve local `HOLDS`,
`SETTLED_FAILED`, checkout/log/container paths and other operator customizations.

After integration, the operator should compare the installed file with the
reviewed canonical file using `diff -u`, save a timestamped backup, and reconcile
customizations into a separate candidate based on the canonical version. Remove
the installed-only direct agent inventory and heartbeat-only orphan block;
keeping it would reintroduce false positives and bypass common read checks.
Review the complete diff and compile the candidate before replacing the installed
file. Run the reviewed checkout's focused check:
`aq test tests/test_supervisor_stall_sweep_script.py`. Then use the existing scoped
patrol invocation to verify classifications. Authorization failures require the
reported scope/token recovery, never token removal or a retry with broader scope.
No daemon restart, capacity/grant/config change or database migration is needed.

The cron entry runs `~/.agent-queue/operator-checks/aq-auto-deploy.sh`, so the
auto-deploy escalation also takes effect only after the same diff, backup and
`install -m 755` of `scripts/operator-checks/aq-auto-deploy.sh`. Its paths,
threshold and recipient default to this box and can be overridden with
`AQ_AUTO_DEPLOY_*` variables.
