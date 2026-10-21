---
tags: [guide, agents, waits]
---

# Verified agent waits

Register a condition once, then end the turn. AQ verifies the source, chooses
whether to retain the process or hibernate, and delivers a durable outcome.
Agents do not choose sleep mode, maintain a polling heartbeat, or choose an
operational timeout. A source being available is not proof that its work passed:
read the result reference and validate its outcome before continuing.

```bash
aq wait register --task producer-task --idempotency-key settlement
aq wait register --kind job --ref JOB_ID --idempotency-key validation
aq wait register --kind message --ref thread-id --after-seq 42 --idempotency-key reply
aq wait register --kind timer --due-at 1800000060 --idempotency-key reminder
aq wait register --condition @condition.json --idempotency-key exact-source
aq wait show WAIT_ID --json
aq wait explain WAIT_ID
aq wait graph --project PROJECT_ID --costs
```

The CLI sends schema version 2 by default, including source shortcuts. Raw CLI,
API and MCP version 1 registration bodies receive `wait.upgrade_required` with a
version 2 example in `detail.example`. `--timeout` is a legacy compatibility
option; use policy deadlines for verified waits. Explicit `--schema-version 1`
is reserved for the documented rollback configuration.

For a message reply, send on a unique thread and register its returned
`created_seq` as the exclusive cursor. Ordinary authenticated sends derive the
sender from the held session; omit sender flags. Use the thread ID rather than
the message ID. Supervisors must reply on that thread (`--reply-to` when using
`aq agent message`) so the verified condition can observe the reply.

```bash
aq message send --to session:supervisor-PROJECT_ID --thread-id decision-1 \
  --body 'Task TASK_ID needs a routing decision.' --json
aq wait register --kind message --ref decision-1 --after-seq SENT_CREATED_SEQ \
  --idempotency-key decision-1
```

Conditions include task settlement, dependency edges, jobs, exact CI check/run
heads, pull requests, batches, messages, timers, human input, scheduler resources
and playbook conditions. Evidence is read from the source owner, never inferred
from an agent's `claimed` commentary. See [source contracts](wait-source-contract.md)
for identities, authorization and stale-source behavior. A registration error
leaves no active wait; correct its source or report the blocker instead of polling.

## Suspension and wake

An in-place wait moves the task to `WAITING` and retains its process and claim.
AQ holds routine mail while waiting. A long wait or capacity pressure can request
hibernation. When asked, write a concise summary and submit the fenced checkpoint:

```bash
aq wait checkpoint --wait-id WAIT_ID --continuation-id CONTINUATION_ID \
  --generation GENERATION --summary-file /tmp/wait-summary.txt
```

Then end the turn. Include the goal, completed changes, next action, constraints,
rejected approaches and uncertainties. AQ records observed branch/HEAD and claim
facts separately. It retains ownership until the exact provider process is
stopped, the task branch published and its checkout detached. A failed stop,
publish or detach holds ownership for recovery. A proven park moves the task to
`HIBERNATED`; an outcome moves it to `RESUME_PENDING`, waiting for fresh capacity.
The new process receives full prime, continuation summary, verified outcome and
held mail. It must acknowledge the delivered generation before ordinary mutations:

```bash
aq wait ack --wait-id WAIT_ID --continuation-id CONTINUATION_ID --generation GENERATION
```

For an in-place outcome, `aq wait show WAIT_ID --consume` acknowledges its
notification. Receipt replay preserves the original registration identity.
`source_unavailable`, cancellation and a failed producer are explicit outcomes;
none implies success. See [wake context](wake-context-compaction.md).

## Deadlines and human input

Operational deadlines escalate to the supervisor without waking the worker or
replacing the wait. The supervisor inspects `aq wait explain`, then records a
reasoned `aq wait decide` decision: extend, wake, cancel, reroute or escalate to a
human. Extension preserves the original wait age and ceiling anchor. Repeated
registration cannot reset the age. Human waits have no automatic deadline and
require hibernation support. The supervisor resolves one with verified human
evidence or an explicit `aq wait lift --response ...`; a worker cannot lift it.

Managed job admission remains opt-in (`resources.jobs.enabled`). Job results and
logs remain under the job owner; inspect `aq job result JOB_ID --json`. Matter
capture and editor pinning are documented in the [object-loop guide](object-loop.md).
During cutover, check the atomic submit-with-wait readiness item in the
[runbook](hibernation-cutover-runbook.md) before enabling an existing installation.

## Operator observations

`aq doctor --check waits.schema` reports revisions, effective flags and row counts;
`aq doctor --check waits.orphans` reports orphan ownership. Repair is conservative:
live ownership and missing checkpoint/head evidence remain held. Fixes retain
continuations, summaries, saved heads and resolved outcomes.

The Metrics page shows pending waits, observed slot time, wake causes and resume
costs. Missing measurements remain null. Released slot time starts at proven
process stop and ends at the first resume grant or report end; pending reservation
time is capacity delay. The 24-hour tile uses these timestamps independently of
chart range. Cost/duration cohorts cover thirty days. Coarse samples average
overlapping snapshots and preserve gaps.

Avoided monitoring tokens are a counterfactual, hidden until an operator supplies
`wait_policy.metrics.pre_rollout_turn_rate`, `pre_rollout_tokens_per_turn`, `cohort`
and `window`. The estimate subtracts measured summary/resume overhead from the
historical rate times observed waiting duration. Missing overhead leaves it null;
this is not measured savings from a polling control arm.
