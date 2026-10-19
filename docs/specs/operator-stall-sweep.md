# Operator stall sweep

`scripts/operator-checks/stall-sweep.py` is a read-only diagnostic for active
projects. `--project PID` confines task, holder and wait observations to that
project. Every daemon read uses the same envelope, authorization and completeness
checks. An authorization denial stops the sweep; failed or partial evidence never
proves either an orphan or a healthy queue.

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
