# System spec: providers

Status: audited against code on 2026-10-22 (base `52d2efe4c`). This is system 12 of the
eighteen in the agent-queue system inventory (vault
`projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`, review
`rev-bright-rapids`, approved at revision 1 when this was written). It explains what the
system is for, how it works today, where it stops, and what is planned. It links to the
existing concept, reference and design documents rather than repeating them.

## 1. Purpose

Every piece of work Agent Queue does ends in a call to a model vendor. Those vendors fail
in ways the rest of the daemon cannot afford to ignore:

- a subscription runs out of its five-hour or weekly window;
- a CLI login expires;
- a vendor has an outage, so every launch dies at startup;
- a rate limit lands in the middle of a task.

A scheduler that keeps launching against a dead provider burns retries, fills the queue
with failures and pages nobody. One that reacts by hand needs an operator awake. This
system owns the answer to "can we use this provider right now, and if not, what happens
to the work waiting on it". It also owns the narrower questions that feed that answer:
how much usage a provider has left, how many tokens the fleet has spent, and how the
daemon calls a model directly when no coding agent is involved.

It has five parts:

- **Availability.** A per-provider state machine fed by typed evidence. The scheduler asks
  it whether a launch is allowed.
- **Failover.** Re-routing queued work off an unavailable or saturated provider, and
  checkpointing in-flight work when its provider fails under it.
- **Usage observation.** Reading each provider's own report of what it has left, so the
  daemon can act before a provider runs dry instead of after.
- **Token accounting and budgets.** The ledger of what was spent, the budgets compared
  against it, and the prompt log.
- **The direct LLM path.** `LLMClient` and its vendor adapters, for the few callers that
  need a completion without a tmux session.

The problems it solves are:

- no launch against a provider known to be unusable, and automatic recovery when it comes
  back;
- queued work keeps moving when one provider is down, inside the routing decision's own
  candidates;
- a provider failure mid-task loses no work and spends no retry;
- an operator learns about outages and expired logins without watching the dashboard;
- the router and allocation controls can weigh live headroom, not just static quotas.

## 2. How it works today

### 2.1 Vocabulary

The concept guide [providers](../../concepts/providers.md) defines the terms. In brief:

- A **provider** is a harness login, not a vendor. `provider_key(harness)` in
  `src/providers/availability.py` returns `harness.base or harness.id`, so the keys are
  `claude`, `codex` and `gemini`. The vendor (`anthropic`, `openai`, `google`) is a display
  alias. The reserved key `llm` stands for the direct path.
- The **worker providers** are the harnesses that get derived worker rungs:
  `WORKER_PROVIDERS` in `src/profiles/catalog.py` lists `claude` and `codex`. Gemini is an
  installed harness with no worker template, so it has availability state but no rungs.
- **Availability state** is one of six values, in two halves. *Launchable*: `available`,
  `degraded`. *Unavailable*: `exhausted`, `unauthenticated`, `failing`, `disabled`.
  `disabled` is only ever an operator override.
- **Provider intent** on a task (`tasks.provider_intent`) says how firmly the task is tied
  to its provider: `pinned`, `preferred` or `class_only`.
- A **usage snapshot** is one reading of a provider's own usage windows (percent used,
  reset time) at one moment.
- The **token ledger** is the record of tokens spent by coding-agent sessions, read from
  their transcripts.

### 2.2 Availability

The state machine is pure code in `src/providers/availability.py`: given the current
record, a piece of evidence, the config and a clock, it returns the next record. It holds
no I/O, so the trip rules are tested as plain functions.

**Evidence** is typed. The kinds include a startup death (strong when the pane shows an
auth or usage-limit screen, generic otherwise), a mid-task `RATE_LIMIT` exit, a usage
snapshot over a threshold, a failed or passed auth probe, a successful authenticated
session, and an operator override. `provider_failover.evidence.keep` (default 20) bounds
the ring of recent evidence each provider keeps; the trip rules count over it.

**Trip rules** (`provider_failover.launch`, `.rate_limit`, `.usage`):

- two strong startup failures, or five generic ones, inside `window_seconds` (600) trip the
  provider to `failing` or `unauthenticated`;
- two rate-limit exits trip it to `exhausted`, until the provider's own `resets_at` or a
  900-second backoff;
- a usage snapshot at 99 percent makes it `exhausted`, 85 percent makes it `degraded`,
  with 2 points of hysteresis so a reading wobbling around a threshold does not flap.

**Recovery** is a half-open breaker (`provider_failover.recovery`). After the reset time
plus a grace period, an unavailable provider enters probation: the next launch is a
canary. A canary that authenticates closes the breaker; one that dies reopens it with
doubled backoff, capped at `backoff_max_seconds` (3600). An `unauthenticated` provider
recovers through the auth probe instead, every 120 seconds. Repeated trips inside
`flap_window_seconds` count as flapping. Notifications are damped while flapping;
flapping alone does not open a human escalation. A canary slot is released when its
session ends, or after the timeout if no terminal session record is available.

**Operator override.** `aq provider set-state` pins a state with an expiry (default four
hours, at most seven days, `provider_failover.override`). A disabled override may be
indefinite; `auto` removes an override and resets failure counters.
`aq provider recheck` runs a fresh auth probe and folds its answer into availability.

**The service.** `ProviderAvailabilityService` (`src/providers/availability_service.py`,
reached as `Orchestrator.provider_availability`) wraps the pure machine:

- it keeps an in-memory snapshot of every provider's record and is the only writer of
  `provider_availability` and `provider_availability_transitions`;
- evidence enters through `record`, `record_startup_death`, `record_rate_limit_exit` and
  `note_session_authenticated`, called from the session layer;
- `tick` runs once per orchestrator cycle: it expires overrides, opens probation, and runs
  the background auth probe (`src/providers/probe.py`, every 600 seconds while launchable);
- `admit_launch` is the scheduler's question. It answers yes for a launchable provider,
  yes-once for a probation canary, and no otherwise;
- every transition emits `provider.state_changed` (which triggers the failover playbook);
  crossing between launchable and unavailable also emits `notify.provider_state` and
  invokes the half-change notifier. Notification tasks are tracked through shutdown.

The mode switch is `provider_failover.mode`: `enforce` (default) refuses launches;
`observe` tracks state, emits events and serves every surface but refuses nothing; `off`
collects no evidence. The service reads config through a getter on every piece of
evidence, so the mode and every threshold are hot-reloadable.

The operator guide is [provider outage](../../guides/provider-outage.md). The design
record, with decisions D1 to D24, is [provider failover](../provider-failover.md).

### 2.3 Failover of queued work

Refusing launches is mechanism and needs no playbook. Moving the work that was waiting is
policy. The split:

- **The engine.** `src/providers/reroute.py` has a pure planner, `plan_sweep`, and
  `ProviderRerouteService`, which applies a plan through task queries
  (`src/database/queries/task_reroute_queries.py`) and writes one `task_reroutes` row per
  move, so every move can be undone.
- **The command.** `provider_reroute` (`src/commands/provider_commands.py`, arguments in
  `src/providers/reroute_args.py`) runs one sweep. `provider_reroute_undo` reverses a move.
- **The policy.** The shipped `provider-failover` playbook
  (`src/prompts/default_playbooks/provider-failover.md`, reviewed bundle in
  `src/prompts/reviewed_playbooks/provider-failover/`) calls `provider_reroute` on every
  `provider.state_changed` and every five minutes, then `provider_availability_notify`.
  It holds no state and decides nothing itself.

A sweep considers unheld READY, DEFINED and BLOCKED tasks whose provider is unavailable,
plus tracked provider-paused tasks. Explicit operator pauses stay paused; moving a BLOCKED
task does not clear its dependency block. For each:

1. A `pinned` task holds with a visible reason. The router writes `pinned` for a hold
   lane; an operator override writes it too.
2. A task the router routed moves only among its recorded `tasks.route.candidates`, to the
   first acceptable target, normally an `available` provider. Candidates may name different
   classes for a narrow lane. The routing decision already said which alternatives were
   acceptable; failover does not invent new ones.
3. The legacy unrouted planner can choose an equivalent class using
   `provider_failover.order` and `.classes` (for example `deep-high: hold`). Current
   mandatory routing holds unrouted tasks for the router; there is no project-default
   route to fall back to.
4. If no target exists, the task holds.

Moves trickle: at most `reroute.max_per_sweep` (10) per sweep, bounded by the target's
backlog, never onto a `degraded` target unless allowed, at most `max_auto_per_task` (2)
automatic moves per task, and a 30-minute per-task cooldown. A moved task records
`rerouted_from` for explicit undo; recovery does not automatically move it back.

**Capacity spill** (`src/providers/spill.py`, D24) is the sweep's second pass. A READY
task that has waited `spill.after_seconds` (300) on a pool at its concurrency limit moves
to a same-class pool with headroom, at most five per sweep. Spill counts against the same
per-task limits as failover moves, and runs only when failover would (`mode: enforce` and
`reroute.enabled`).

The daemon reconciles the required system activation on startup. If an operator opts out
or the activation is unhealthy, tasks on an unavailable provider hold with a reason and
nothing moves. That is the safe failure; `aq doctor --check providers.failover_playbook`
reports it.

### 2.4 Failover of in-flight work

When a session dies mid-task, `src/providers/inflight.py` decides whether its provider is
to blame. The verdict is one of:

- **tripped**: the exit is attributed and the provider is (now) unavailable;
- **suspect**: the exit looks like a provider failure but has not tripped the provider;
- **unattributed**: an ordinary failure.

`ProviderFailoverMixin` (`src/orchestrator/provider_failover.py`) acts on it. For a tripped
or suspect exit it commits the worktree as `aq-wip: provider failover checkpoint`, pushes
it, and writes a handoff note to `task_metadata['provider_failover_handoff']` so the next
agent, on whatever provider, starts from the checkpoint and knows why. On a train or
hierarchy, it instead checkpoints only the task's own worker/repair writer branch with
`WIP saved by AQ: …`, fast-forwards it and records a resume point. A branch owned by
another writer is preserved for integration recovery. The handoff also goes in a task
comment and includes at most 80 pane lines / 8192 bytes (2048 bytes in prime). An attributed
failure spends no retry. In `enforce` mode the first suspect exit pauses the task for
`launch.suspect_backoff_seconds` (30) as `provider_suspect`, so a burst of failures trips
the provider before the task is relaunched into it. If the checkpoint push fails, the task
is held with `needs_attention: provider_failover_push_failed` rather than relaunched
without its work.

The session layer feeds this. `src/sessions/usage_limit_screen.py` recognises usage-limit
and login screens in a pane, `src/sessions/exit_classifier.py` classifies exits, and
`src/sessions/reconciler.py` reports deaths to the availability service.

### 2.5 Allocation controls

`src/providers/allocation.py` gives the operator a provider-level view of how many workers
each provider may run. `aq pool provider status` shows each provider's pools,
configured and effective worker counts, live usage and availability. `preview` plans a
change (`plan_allocation_preview`) and stores it in a short-lived preview registry;
`apply` applies a stored preview by its token. The commands are in
`src/commands/provider_allocation_commands.py`. Pool sizing itself belongs to
scheduling-and-claims; this module only proposes changes to it.

The design is the vault spec `specs/provider-worker-allocation-controls.md` (status
`approved`, task `brisk-cascade-85`, 2026-09-04) and the plan
`plans/2026-09-27-provider-worker-allocation-controls.md` (approved by Jack in chat
2026-09-27). Epic `nimble-bridge` implemented it and is complete.

### 2.6 Usage observation

Each provider reports its own usage differently, so there is one reader per provider:

- **Codex** is passive. Its transcripts carry `rate_limits` on `token_count` lines; the
  transcript reader turns them into snapshots as sessions run.
- **Claude** is active. Claude publishes no local rate-limit file, so the shipped
  `provider-usage-probe` playbook calls the `provider_usage_probe` command every ten
  minutes. The command runs `claude -p "/usage" --output-format json` with a 20-second
  ceiling (never `--bare`, which drops the login) and parses the text with
  `src/providers/claude_usage.py`. Its system activation is required and reconciled at
  startup unless explicitly opted out. The probe is free: the CLI reports `num_turns: 0` and
  `total_cost_usd: 0`. Its last verdict is kept in system config
  `providers.claude_usage.last_probe`.
- **Gemini** has no reader, so it has no usage data (§5).

Readings are stored as `ProviderUsageSnapshot` rows in `provider_usage_snapshots`
(`src/database/queries/provider_usage_queries.py`). The table is append-only; a reading
identical to the newest stored one is dropped and only its `last_seen_at` moves, so an
idle account writes nothing. Staleness is computed from `last_seen_at` against
`providers.claude.stale_after_seconds` and `providers.codex_stale_after_seconds`.
`GET /api/providers/usage` (`src/api/providers.py`) computes it server-side so every
client agrees.

Three consumers read snapshots: the availability machine (thresholds, §2.2), the router's
headroom weighting (routing-and-profiles), and the allocation status view. The designs
are [provider usage](../../superpowers/specs/2026-09-07-provider-usage-design.md) and its
[implementation plan](../../superpowers/specs/2026-09-07-provider-usage-implementation.md).

### 2.7 Token accounting and budgets

The reference is [usage accounting](../../reference/usage-accounting.md). The essentials:

- **The ledger.** `token_ledger` is normally written by the transcript watcher
  (`src/sessions/transcripts/watcher.py`), which reads each session's transcript
  (`claude.py`, `codex.py` beside it) and records per-call usage, with call detail in
  `transcript_usage_calls`. The legacy synchronous workflow also records total-only
  output usage. Queries are in `src/database/queries/token_queries.py`.
- **Three qualities.** A count is `observed` (the provider reported it), `estimated`
  (characters divided by four; only ever in `llm.jsonl` as `*_est` fields), or
  `unavailable`. `TokenUsage.reported` (`src/llm/types.py`) says which. A hard budget
  never trusts an estimate: it fails closed.
- **Budgets.** `global_token_budget_daily` and a project's `budget_limit` are compared
  against the ledger. When either is reached, a claim answers `not_admissible` with
  `budget_exhausted`. `BudgetManager` (`src/tokens/budget.py`) is the single source of the
  global budget figure. The scheduler side is in
  [scheduler and budget](../scheduler-and-budget.md).
- **Playbook budgets.** A playbook `llm` step is the only call with a hard per-call budget
  (`AiBudget`: `max_calls`, `max_output_tokens`, `max_total_tokens`, `timeout_seconds`),
  and returns a typed outcome when it is exceeded.
- **Costs.** `aq costs` (`src/commands/ops_commands.py`) prices ledger rows that have a
  model and an input/output split, using the `pricing:` table (`PricingConfig` in
  `src/config.py`). The table ships empty.
- **The prompt log.** `LLMLogger` (`src/llm_logger.py`) writes direct-path calls as
  date-partitioned JSONL under `logs/llm/<date>/`, flushes hourly `PromptAnalytics`, and
  prunes after `llm_logging.retention_days`. Spec: [LLM logging](../llm-logging.md).

### 2.8 The direct LLM path

Most work runs as a coding agent in tmux (sessions-and-harnesses). A few callers need a
completion without a session:

- playbook `llm` steps;
- a plugin's `invoke_llm`;
- the reference-stub enricher;
- `aq vault rebuild-index --with-summaries`.

They use `LLMClient` (`src/llm/client.py`), with `complete()` for one completion and
`run_tools()` for a bounded tool loop. Adapters live in `src/llm/providers/`:

- `anthropic.py`, whose credential order is Vertex, then Bedrock, then API key, then the
  Claude Code OAuth token;
- `google.py`;
- `openai.py`, which also serves local OpenAI-compatible endpoints (`local_probe.py`).

An adapter's `reports_usage` defaults to false, so its counts are `unavailable` unless it
says otherwise. Model resolution (`src/llm/spec.py`) is `spec.model`, then the step's
intelligence class, then `llm.model`, then the adapter default. On this path the provider
for API playbook steps is `llm.provider`; other callers can set `spec.provider`.
`llm.fallback` supplies a second credential block when the primary gate is unavailable.
An incompatible class slice or explicit model refuses fallback; successful fallback
calls do not prove primary recovery. Adapter caches distinguish primary and fallback
credentials even for the same vendor/model/endpoint. `src/llm/fake.py` is the test
double.

`src/llm/cli.py` (`ask_cli`) is a newer one-shot path: a tool-free call through a
logged-in agent CLI, in an empty temporary directory, with no task and no session. It lets
a playbook step use a subscription login instead of an API key with `transport: cli`.
The profile must select a harness and class and must not enable tools. This path uses
neither the direct API availability gate nor `llm.fallback`, `llm.jsonl` or the ledger.
It does enforce the step budget and fails closed when reported token usage is missing.

The design is [LLM direct path](../../superpowers/specs/2026-08-30-llm-direct-path-design.md);
the operator guide is [LLM providers](../../guides/llm-providers.md).

### 2.9 Data model

| Table | Holds | Writer |
|---|---|---|
| `provider_availability` | one row per provider: state, reason, evidence ring, reset time, override, probation | availability service only |
| `provider_availability_transitions` | append-only history of state changes | availability service only |
| `provider_usage_snapshots` | append-only usage readings, deduplicated, with `last_seen_at` | transcript reader, usage probe |
| `task_reroutes` | one row per failover or spill move, for history and undo | reroute engine |
| `token_ledger`, `transcript_usage_calls` | tokens spent per session call | transcript watcher; legacy sync workflow also writes total-only ledger rows |
| `tasks.provider_intent`, `tasks.rerouted_from`, `tasks.route` | how firmly a task is tied to its provider, and where it came from | router, override, reroute engine |

The table definitions are in `src/database/tables.py`; `archived_tasks` mirrors the task
columns.

### 2.10 Surfaces

- **CLI:** `aq provider status|history|held-tasks|recheck|set-state|reroute|reroute-undo`,
  `aq pool provider status|preview|apply`, `aq costs`.
- **Commands:** `src/commands/provider_commands.py`,
  `src/commands/provider_allocation_commands.py`, `src/commands/ops_commands.py`.
- **API:** `src/api/providers.py` (availability, usage, allocation preview/apply), `src/api/metrics.py` (tokens).
- **Doctor:** `providers.availability`, `providers.recovery_stuck`,
  `providers.failover_playbook`, `providers.held_tasks` (warns after
  `doctor.held_warn_seconds`, four hours), `providers.claude_usage`,
  `providers.usage_activity_gap` (active provider without readings for 30 minutes).
- **Config:** `providers:` (usage probe and staleness), `provider_failover:` (§2.2–2.4),
  `llm:` and `llm.fallback`, `pricing:`, `llm_logging:`. All are in `src/config.py`; the
  reference is [config](../config.md).

The module catalogue is [providers](../../reference/modules/providers.md).

## 3. Boundaries

| System | What crosses the boundary |
|---|---|
| daemon-core | `run_one_cycle` calls the availability `tick` once per cycle; daemon-core owns the ordering. |
| scheduling-and-claims | The pool scheduler asks `admit_launch` before starting a session. Claims answer `budget_exhausted` from the budgets here. Pool sizes belong there; allocation controls only propose changes. |
| routing-and-profiles | The router writes `route`, `route.candidates` and `provider_intent`, and reads usage headroom. Failover moves a task only among those candidates. Intelligence classes and rungs belong there. |
| sessions-and-harnesses | Sessions supply the evidence: startup deaths, exits, pane screens, transcripts. Harness files define the logins that are this system's provider keys. |
| projects-workspaces-git | The in-flight checkpoint commits and pushes through `GitManager`. |
| playbooks | The two shipped policy playbooks call this system's commands; playbook `llm` steps are the main direct-path caller and the only hard per-call budget. |
| messaging-and-human-asks | Provider escalations and `notify.provider_state` reach the supervisor, digest and Discord. |
| supervisors-and-policy | Supervisors are notified of state changes and may override state through commands. |
| configuration-and-vault | All config sections in §2.10; the vault holds intelligence-class files the failover target lookup reads. |
| api-and-clients, dashboard | Availability, usage and token endpoints; the dashboard renders them and computes nothing. |
| plugins-mcp-and-tools | Plugins reach the direct path through `invoke_llm`. |
| operations-and-recovery | Doctor checks; the operator guide for outages. |
| task-graph | Reroute writes task columns through task queries; it does not change hierarchy. |

What this system does not own: which profile a task gets (routing), how many sessions run
(scheduling), how a harness is launched (sessions), or what an agent does once launched.

## 4. Principles and invariants

- **A provider is a login.** State is kept per harness login, because that is what fails:
  an expired Claude login does not affect Codex, even when both bill the same vendor.
- **Mechanism in code, policy in playbooks.** Refusing a launch to a dead provider is
  mechanism and happens without any playbook. Moving work is policy and happens only
  through the `provider_reroute` command called by a playbook. A missing playbook leaves
  work held and visible, never moved by hidden code.
- **One writer per table.** Only the availability service writes availability state; the
  transcript watcher owns per-call ledger ingestion (the legacy sync path can record
  totals); only the reroute engine writes `task_reroutes`.
- **The state machine is pure.** `availability.py`, `reroute.plan_sweep`, `inflight.py`
  and the spill planner take state in and return decisions out, with no I/O.
- **Evidence, not guesses.** State changes come from typed evidence and named trip rules,
  with hysteresis and a half-open breaker. Recovery is proved by a canary, not assumed.
- **Failover respects the routing decision.** A routed task moves only among its own
  candidates; a pinned task never moves automatically.
- **Moves are bounded and reversible.** Per-sweep caps, per-task limits and cooldowns stop
  thrash, and every move has a `task_reroutes` row to undo it.
- **No work is lost to a provider.** An in-flight provider failure checkpoints and pushes
  before anything relaunches, holds if the push fails, and spends no retry.
- **Hard budgets fail closed.** An estimated or unavailable count never satisfies a hard
  budget.
- **Observe before enforce.** `observe` mode exercises every surface without refusing a
  launch, so new rules can be watched before they bite.
- **Hot-reloadable.** Thresholds and the mode are read on every piece of evidence.

## 5. Known gaps

**Behaviour.**

- **Anthropic `xhigh` is not mapped on the API path.** Shipped high classes request
  `thinking: xhigh`, but `_anthropic_budget` maps only `off`, `low`, `medium` and
  `high`; an unknown value resolves to zero. API calls therefore disable thinking for
  those classes. The CLI path forwards the effort setting. This is a suspected code bug,
  not an intended class policy.

- **Direct-path tokens are not ledgered.** `token_ledger` covers sessions only. An API playbook
  `llm` step or `invoke_llm` call is logged to `llm.jsonl` but counts toward no daily or
  project budget. Headless CLI steps enter neither the log nor the ledger.
- **Gemini has no usage reader and no transcript reader.** It gets availability state from
  startup evidence but no snapshots, no ledger rows and no worker rungs.
- **Exhaustion detection leans on pane text.** `usage_limit_screen.py` matches the CLIs'
  current wording. A CLI release that rewords its limit screen degrades strong evidence to
  generic until the patterns are updated.
- **The Claude probe parses free text.** `claude_usage.py` matches limit lines by regex;
  a format change yields `unparsed`. `aq doctor --check providers.claude_usage` reports
  it but nothing repairs it.
- **Budget helpers without callers.** `BudgetManager`'s ratio and deficit helpers have no
  production caller; budgets are a hard cutoff, not a pacing signal.
- **Costs need a pricing table.** `pricing:` ships empty, so `aq costs` prices nothing
  until an operator fills it in.
- **Prompt analytics can be lost.** `PromptAnalytics` is held in memory and flushed
  hourly; a restart drops the partial hour.
- **Credential order can surprise.** A stray `AWS_REGION` in the daemon's environment
  sends Anthropic direct-path calls to Bedrock before an API key is tried.
- **Failover depends on an active playbook.** By design, but a project that disables or
  forks `provider-failover` badly gets held work and only a doctor warning.

## 6. Planned work

This is a historical planning snapshot from 2026-10-20, not a current task-status query.
At that time, no open epic was about providers directly; the
system's own epics (`bold-rapids` failover, `nimble-bridge` allocation, `fresh-rapids-73`
handoff on exhaustion, `grand-ember-77` headroom routing, `vivid-stone-92` pane
detection, `steady-horizon-12` recovery flapping, `bright-ridge-85` expired runtime login)
were complete. The recorded open work was:

- **`clear-impact-72.9` (READY): evaluate and plan a rewrite of `provider-failover`** onto
  the reusable playbook building blocks. It is part of `clear-impact-72` (IN_PROGRESS),
  the umbrella Jack asked for on 2026-10-20 to evaluate every shipped playbook. It waits
  for the playbooks refactor (`bright-grove-21`, delivered through `agile-stone-80`) to
  reach dev, reads the vault spec
  `specs/2026-10-12-implementation-playbooks-reusable-building-blocks.md`, and uses
  `stuck-task-watch` as the reference conversion. It writes
  `plans/2026-10-20-playbook-rewrite-provider-failover.md` in the vault and files one
  implementation task, or records why a rewrite is not worth it. It does not change the
  playbook. Because the playbook is two command calls, the likely change is small: its
  steps become building blocks, and the reroute rules stay in the command.
- **`clear-impact-72.10` (READY): the same for `provider-usage-probe`**, with the plan at
  `plans/2026-10-20-playbook-rewrite-provider-usage-probe.md`.
- **Adjacent: `stark-willow-95.13` (READY)** builds one `AdmissionView` shared by the
  frontier, pools, `aq task explain` and delivery guards (vault fast-smooth-integration
  spec, revision 2). Provider availability is one input to admission, so this changes
  where `admit_launch`'s answer is read and explained, not how it is computed.
- **Adjacent, blocked:** `wise-orbit-24` (playbooks F3 activation) and `agile-vault-38`
  (playbook scripts refused by default) affect how the two policy playbooks are activated
  and what they may run.

The snapshot identified ledgering direct-path calls and a Gemini usage reader as
candidates for filing. The documentation gaps recorded then were corrected in the
2026-10-22 audit; the behavioural gaps in §5 remain.

## 7. Related documents and source

Documents:

- [Providers concept](../../concepts/providers.md)
- [Scheduling: provider availability and failover](../../concepts/scheduling.md#provider-availability-and-failover)
- [Provider outage guide](../../guides/provider-outage.md)
- [LLM providers guide](../../guides/llm-providers.md)
- [Provider failover design record](../provider-failover.md) (D1–D24)
- [Scheduler and budget](../scheduler-and-budget.md)
- [LLM logging](../llm-logging.md)
- [Usage accounting reference](../../reference/usage-accounting.md)
- [Providers module catalogue](../../reference/modules/providers.md)
- [Provider usage design](../../superpowers/specs/2026-09-07-provider-usage-design.md) and
  [implementation](../../superpowers/specs/2026-09-07-provider-usage-implementation.md)
- [LLM direct path design](../../superpowers/specs/2026-08-30-llm-direct-path-design.md)
- [Usage-aware concurrency](../../superpowers/specs/2026-08-24-usage-aware-concurrency.md)
- Vault: `specs/provider-worker-allocation-controls.md` (approved),
  `plans/2026-09-27-provider-worker-allocation-controls.md` (approved),
  `specs/2026-09-28-mandatory-task-routing.md`

Source:

- `src/providers/`: `availability.py`, `availability_service.py`, `probe.py`,
  `inflight.py`, `intent.py`, `reroute.py`, `reroute_args.py`, `spill.py`,
  `allocation.py`, `claude_usage.py`, `snapshot.py`
- `src/orchestrator/provider_failover.py`
- `src/sessions/usage_limit_screen.py`, `exit_classifier.py`, `reconciler.py`,
  `transcripts/`
- `src/llm/` (`client.py`, `cli.py`, `spec.py`, `types.py`, `fake.py`, `providers/`),
  `src/llm_logger.py`, `src/tokens/budget.py`
- `src/commands/provider_commands.py`, `provider_allocation_commands.py`,
  `ops_commands.py`
- `src/api/providers.py`, `src/api/metrics.py`
- `src/database/queries/provider_availability_queries.py`, `provider_usage_queries.py`,
  `task_reroute_queries.py`, `token_queries.py`
- `src/prompts/default_playbooks/provider-failover.md`, `provider-usage-probe.md` and
  their bundles in `src/prompts/reviewed_playbooks/`
