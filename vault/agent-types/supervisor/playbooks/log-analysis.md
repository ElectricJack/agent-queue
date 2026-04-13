---
id: log-analysis
triggers:
  - schedule.hourly
scope: agent-type:supervisor
cooldown: 3600
max_tokens: 30000
---

# Log Analysis

Periodically scan recent system logs and event history for anomalies,
error patterns, performance issues, and resource waste. Write actionable
operational insights to orchestrator memory so the system can self-correct
over time.

This playbook is the operational counterpart to the reflection playbook.
While reflection focuses on individual task outcomes and agent learning,
log analysis looks at the system as a whole — the orchestrator, scheduling
decisions, token budgets, error rates, and infrastructure health.

## Gather recent events and logs

Start by reading the recent event log using `get_recent_events` with
`since: "1h"` to scope events to the last hour.  Pull up to 50 events
and group them by event type.  Use the `event_type` filter to drill into
specific categories if the initial scan is noisy (e.g.
`event_type: "task.*"` for task lifecycle events only).

Also run `read_logs` with `level: "warning"` and `since: "1h"` to
surface warnings and errors from the daemon's structured log file.  For
deeper investigation, narrow with `component`, `project_id`, or
`pattern` filters.

Run `token_audit` to get a breakdown of token usage across projects and
agents over the last 24 hours.

Check the system status using `get_status` to understand the current
state — how many agents are running, how many tasks are queued, and
whether any projects are paused or over budget.

Build a mental picture of what the system has been doing since the last
analysis run.

## Scan for error patterns

Look through the events for recurring problems:

- **Task failures:** Are tasks failing repeatedly? Is the same task
  being retried and failing each time? A task that has failed 3+ times
  likely has a systemic issue (bad task description, missing dependency,
  environment problem) rather than a transient error.
- **Agent questions:** Frequent `agent_question` events suggest tasks
  are underspecified. If the same type of question keeps appearing,
  the task templates or project context may need improvement.
- **Chain stuck / stuck defined tasks:** These indicate dependency
  resolution problems or tasks that can't proceed. Check if the
  blocking tasks are themselves stuck, creating cascading delays.
- **Budget warnings:** Repeated `budget_warning` events mean a project
  is consistently hitting its token limit. This could indicate tasks
  are too large and need splitting, or the budget needs adjustment.
- **Merge conflicts and push failures:** VCS errors suggest agents
  are stepping on each other's work, or the branching strategy needs
  attention.
- **Approval stuck:** Plans waiting too long for human approval create
  bottlenecks. Track how long plans sit in the approval queue.
- **Playbook failures:** `playbook_compilation_failed` or playbook run
  failures indicate problems with the self-improvement infrastructure
  itself.

For each pattern found, note the frequency, affected projects and agents,
and the time window over which it occurred.

## Analyze token usage and efficiency

Using the token audit data, look for efficiency signals:

- **Expensive tasks:** Tasks that consumed disproportionately many
  tokens relative to their output (small diffs, few files changed).
  These may indicate vague task descriptions, agents exploring dead
  ends, or tasks that should have been split.
- **Cheap, fast tasks:** Tasks that completed efficiently. What made
  them work well? Good descriptions, prior memory, small scope?
- **Project imbalance:** Is one project consuming far more tokens than
  its fair share? This might be fine (complex project) or might
  indicate runaway tasks.
- **Idle agents:** Agents that aren't being utilized. Are there enough
  tasks queued? Is the scheduler correctly distributing work?
- **Token trends:** Is overall usage trending up or down? Sudden spikes
  may indicate a problem. Gradual increases may just mean more work.

## Identify operational anomalies

Beyond specific error types, look for things that are unusual or
unexpected:

- **Timing anomalies:** Tasks that took much longer than similar past
  tasks. Agents that are idle for extended periods despite queued work.
- **Event gaps:** Long periods with no events may indicate the system
  was down or stuck.
- **Repeated restarts:** Multiple `system_online` events in a short
  window suggest instability.
- **Scheduling issues:** Tasks being assigned to agents that aren't
  well-suited for them, or projects not getting their fair share of
  agent time.

## Write operational insights to memory

For each significant finding, save it to orchestrator memory using
`memory_store`. Each insight should be:

- **Specific and actionable** — not "there were some errors" but
  "project X had 5 task failures in 4 hours, all with import errors
  suggesting a broken virtualenv"
- **Tagged with category** — use tags like `#error-pattern`,
  `#token-efficiency`, `#scheduling`, `#infrastructure`,
  `#budget`, `#anomaly`, `#bottleneck`
- **Timestamped with analysis window** — include the time range
  this analysis covers so future runs can pick up where this one
  left off

Prioritize insights that are actionable. The goal is not to log
everything, but to surface the things a human operator or the
orchestrator should act on.

Do not save findings that are clearly transient (a single retry that
succeeded, a brief network hiccup) unless they form part of a larger
pattern.

## Check against known patterns

Before writing new insights, search orchestrator memory for existing
operational knowledge. If a finding matches a known pattern:

- **Confirm it** — bump confidence from `#provisional` to `#verified`
  if this is the second or third observation
- **Update it** — add the new occurrence data, update frequency counts
- **Escalate it** — if a `#provisional` pattern keeps recurring, it
  may warrant a notification to the human operator

If a previously identified pattern is no longer appearing, note that
too — the issue may have been resolved. Consider updating or archiving
the old insight.

## Skip conditions

If the event log shows normal operation with no errors, no anomalies,
and token usage within expected ranges, skip the analysis. Not every
run produces findings worth capturing.

If the system has been idle (very few events), skip as well — there's
nothing meaningful to analyze.
