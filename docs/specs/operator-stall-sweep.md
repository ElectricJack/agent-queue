# Operator stall sweep

`scripts/operator-checks/stall-sweep.py` is a read-only diagnostic for active
projects. `--project PID` confines task, holder and wait observations to that
project. Every daemon read uses the same envelope, authorization and completeness
checks. An authorization denial stops the sweep; failed or partial evidence never
proves either an orphan or a healthy queue.

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
