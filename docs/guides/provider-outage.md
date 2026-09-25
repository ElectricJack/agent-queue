---
tags: [guide, providers, failover, operations]
---

# A provider ran out of usage

What to do when Claude, Codex or Gemini runs out of usage, loses its login or
keeps failing to start: how to read what AQ has noticed, what it already did
about it, and the handful of decisions it leaves to you.

## Why it exists

A coding-agent CLI runs on an account with limits. When one runs dry
mid-afternoon, every session on it dies or parks on a "you've hit your limit"
screen, and every launch against it fails. Without failover, those tasks retry
into the dead provider, spend their retry budget and end up paused.

AQ now tracks each provider's **availability** and stops launching against one
that cannot do work. A shipped playbook then moves waiting work to the same
intelligence class on a provider that can. Most outages need nothing from you:
the provider resets and work flows again. This guide is for the ones that do —
a logged-out CLI, a false alarm, a task you want moved by hand — and for
checking that the automatic part is actually switched on.

## Vocabulary

* **Provider** — one harness login: `claude`, `codex` or `gemini`, plus the
  reserved key `llm` for the daemon's direct API calls. Commands accept a
  vendor name (`anthropic`, `openai`, `google`) as an alias. A harness variant
  declared with `base:` shares its parent's login, so it shares its
  availability too ([src/providers/availability.py](../../src/providers/availability.py)).
* **State** — one of six, in two halves. *Launchable*: `available`,
  `degraded`. *Unavailable*: `exhausted`, `unauthenticated`, `failing`,
  `disabled`. Nothing launches against an unavailable provider.
* **Hold** — a queued task whose provider is unavailable. It keeps its status
  (`READY` stays `READY`); the hold is derived, never stored, and says *why* the
  task is not moving (its `kind`).
* **Provider intent** — whether anyone meant the provider a task's profile
  names: `pinned` (hold during an outage), `preferred` or `class_only` (fail
  over). See [scheduling](../concepts/scheduling.md#provider-availability-and-failover).
* **Re-route** — changing a queued task's profile to the same class on another
  provider. Every automatic move of one outage shares a **batch** id
  `prb-<provider>-<generation>`; operator moves get their own `prf-…` batch.
* **Probation** — the way back: an unavailable provider becomes `degraded`
  with reason code `recovering`, and one launch (the canary) must succeed
  before it is `available` again.

## A realistic example

Codex hits its weekly limit at 14:10. You see the banner on the dashboard, or a
message in your inbox headed *Provider codex: exhausted*. Assuming the daemon
is running and you are the operator:

```bash
aq provider status                       # the state, reason and expected recovery
aq provider held-tasks                   # what it is holding, and why each one is not moving
aq provider reroute --dry-run            # what the next sweep would do — writes nothing
```

`status` shows `codex (openai)` as `exhausted`, *Recovery in 2.3h* (the reset
time Codex itself reported), `Held` and `Moved` counts, and the reason — for
example `secondary window at 100%` (Codex's longer window). `held-tasks`
lists each held task with its hold kind: `awaiting_failover_capacity (3 ahead)` for work
trickling across to Claude, `provider_pinned` for a task a human tied to
Codex, `no_equivalent_rung` for an `astra-high` task that only Codex can run.

That is usually the whole incident: the Claude pool picks up a pool-width of
moved work at a time, the pinned and Astra tasks wait, and at the reset the
provider goes on probation, the next Codex launch succeeds, and it is
`available` again. The rest of this page is for when you want to intervene.

> **Examples on this page** use flags checked against `aq provider <command>
> --help` on the current tree; the output described is what the commands
> return, not a captured session. `aq provider --help` is always authoritative.

## How you find out

| Where | What you see |
|---|---|
| Dashboard | A banner on every page while any provider is unavailable. The Metrics tab's provider cards show the state pill, reason, since, a countdown to the expected recovery, an override badge, held/re-routed counts, and *Disable for…*, *Recheck* and *Clear override* actions. Task detail shows the intent chip (*Pinned* / *Preferred* / *Class only*), "re-routed from X · undo" and the hold reason; the Tasks tab has a *Held by provider* filter. |
| Inbox | One message to the global supervisor and to you (`user:dashboard`) each time a provider crosses between the halves — not for every flicker inside one half. Each project's supervisor also gets one notice per re-route batch listing what moved and what held. |
| Discord | Only when a human has to act — see [what reaches Discord](#what-reaches-discord). The hourly digest also carries provider changes as fleet facts. |
| CLI | `aq provider status`; `aq task explain --task-id <id>` shows a `provider_hold` reason for a held task. |
| Doctor | `aq doctor --check providers.availability` (and the three other `providers.*` checks — see [troubleshooting](#common-failures-and-recovery)). |

## Read the state

`aq provider status` prints one row per tracked provider; `--provider codex`
narrows it and `--verbose` adds the evidence ring and the last ten
transitions. `aq provider history --provider codex` is the full transition
log, newest first, with who caused each change (`system` or an operator).

| Column | Meaning |
|---|---|
| State | The effective state. `(override, in 3.8h)` means an operator set it and when that expires. |
| Since | When it entered that state. |
| Recovery | When AQ expects it back (`until`): the provider's own reset time when it reported one, otherwise a backoff. `—` means nothing will bring it back on its own — typically a logout. `due` means the time has passed and probation is imminent. |
| Held | `READY` tasks routed to it. `aq provider held-tasks` is the itemised list, and also covers work that is not ready yet. |
| Moved | Tasks the current outage's batch has re-routed off it and not undone. |
| Last OK | The last launch that succeeded. |
| Usage | The newest account-wide usage reading, e.g. `secondary 100%` for Codex or `session 92%` for Claude. |
| Reason | Why, in words. A remediation line follows the table for every unavailable provider. |

What each state means and what normally ends it:

| State | Reason codes you will see | What it means | What ends it |
|---|---|---|---|
| `available` | — | Healthy. | — |
| `degraded` | `usage_high`, `model_scope_exhausted`, `suspect_usage`, `suspect_auth`, `probe_not_authenticated`, `rate_limit_exit`, `launch_failures_unattributed`, `recovering` | Still launched against, but **not a failover target**. Usually one signal awaiting corroboration, or usage at or above `provider_failover.usage.degraded_percent` (85 %). `recovering` is probation. | The signal clears, or it trips. |
| `exhausted` | `usage_exhausted`, `usage_dialog`, `rate_limited`, `llm_quota`, `canary_failed` | Out of usage: a usage window at or above `usage.exhausted_percent` (99 %), two launches dying on the usage-limit dialog, or two sessions exiting on a rate limit inside `launch.window_seconds`. | The reset time passing (plus `recovery.reset_grace_seconds`), a fresh usage reading below the degraded threshold, or a successful call. |
| `unauthenticated` | `login_required`, `probe_not_authenticated`, `llm_auth_rejected` | The CLI is logged out: a launch died on its login dialog and the login probe agrees, two launches in a row died on it, or the probe said *not signed in* twice. | A login probe answering *signed in* — every `recovery.auth_probe_interval_seconds` (120 s), or now with `aq provider recheck`. |
| `failing` | `launch_failures`, `canary_failed` | `launch.generic_failures_to_trip` (5) launches in a row died during startup for a reason AQ cannot name, across two projects or in one where another provider launched fine. | The backoff passing (300 s, doubling on each failed canary up to 3600 s), or a successful call. |
| `disabled` | `operator_disabled` | An operator took it out of service. | The override expiring, or `set-state --state auto`. |

A single unexplained failure never takes a provider out of service: a trip
needs the provider's own structured statement (a usage reading, the login
probe) or two independent signals. The thresholds are all
[`provider_failover.*` settings](../reference/configuration.md#provider_failover-keys).

## What AQ does on its own

Under `provider_failover.mode: enforce` (the default):

1. **It stops launching against the provider.** The scheduler skips its
   workers, its pools are sized to zero, a pre-launch check refuses any launch
   that races the change, and an idle pool worker on it is told to drain on its
   next claim. Sessions already running keep running — one still making turns
   is evidence the provider is fine.
2. **Queued tasks stay queued.** A `READY` task stays `READY`, with a
   `provider_hold` reason in `aq task explain`. Nothing is paused, failed or
   charged a retry.
3. **In-flight work is preserved.** A session that dies on its provider (a
   rate-limit exit, a usage-limit or login screen) spends no retry. Before its
   workspace is released AQ commits uncommitted work as
   `aq-wip: provider failover checkpoint`, pushes the task branch and leaves a
   hand-off note that the next worker's `aq prime` shows. A CLI *parked* on its
   usage-limit screen is stopped by the first stall rung and treated the same
   way ([sessions](../concepts/sessions.md#the-stall-ladder)). If the push
   fails, the task is held in place instead of moved (see
   [troubleshooting](#common-failures-and-recovery)).
4. **The `provider-failover` playbook moves waiting work.** On every state
   change and every five minutes it calls `provider_reroute`, which moves each
   eligible queued task to the **same class on the next available provider**,
   a pool-width at a time, and records the move on the task (a comment,
   `rerouted_from`, a `task_reroutes` row). What it will not move, it holds with
   a reason. See the
   [fallback rule](../concepts/scheduling.md#the-fallback-rule).
5. **Unrouted tasks follow the project default's twin.** A task with no
   profile of its own runs on the project default; while that default's
   provider is down, it resolves to the same class on an available provider.
   This is derived per decision and never written, so recovery needs no undo.
6. **It tells people.** One message per half change, one notice per batch per
   project, and an escalation when a human has to act.
7. **It recovers on its own.** At the reset time, or when the login probe says
   signed in, the provider goes on probation and the next successful launch
   makes it `available`.

The daemon's own direct API calls (playbook `llm` steps, routing) are tracked
under the `llm` key. While it is unavailable they fail fast rather than wait,
unless you configured a second credential in
[`llm.fallback`](../reference/configuration.md#llmfallback).

In `observe` mode AQ tracks, reports and notifies but suppresses nothing and
moves nothing on its own; `off` records nothing at all.

## Decide what to do

Start every intervention with a dry run: `aq provider reroute --dry-run`
returns exactly the plan the sweep would apply — `moved`, `held` (with
`held_by_kind`), `skipped` — and writes nothing.

| Situation | What to do |
|---|---|
| `exhausted` with a recovery time | Nothing. Wait for the reset. Work that can move is moving; pinned work, single-provider classes and anything past the trickle wait. |
| `unauthenticated` | [Log in again, then recheck](#log-in-again). |
| You want a provider out of service for a while | [Disable it with an expiry](#take-a-provider-out-of-service). |
| AQ thinks it is down but it is not | [Force it available](#a-false-alarm). |
| A pinned task must run now | [Force-move it](#move-a-task-by-hand). |
| Tasks paused before failover existed are stuck on a dead provider | [Include them in a sweep](#move-tasks-that-were-paused-before-failover). |
| A move was wrong | [Undo it](#undo-a-re-route). |

### Log in again

The remediation line in `aq provider status` names the login command for that
harness (for example `codex login`). Run it on the daemon host as the user the
daemon runs as, then fold the result in straight away:

```bash
aq provider recheck --provider codex
```

A *signed in* answer puts the provider on probation; the next launch is the
canary. Without `recheck`, the daemon probes an unauthenticated provider every
`recovery.auth_probe_interval_seconds` (120 s) anyway. If the account is also
out of usage, the login will not stick until the window resets.

### Take a provider out of service

```bash
aq provider set-state --provider codex --state disabled --for 4h \
  --reason "codex account under review"
```

`--for` takes `90s`, `30m`, `4h`, `2d` or seconds; `--until` takes epoch
seconds or ISO-8601. With neither, the override lasts
`provider_failover.override.default_ttl_seconds` (4 h); it can never exceed
`override.max_ttl_seconds` (7 d). `--no-expiry` is accepted for `disabled`
only. A reason is required. Disabling is an outage like any other: the sweep
moves its queued work. It never pages anyone and `aq doctor` reports it as
INFO, because you chose it.

End it early with:

```bash
aq provider set-state --provider codex --state auto
```

`auto` clears the override, resets the failure counters and re-derives the
state from evidence. A provider whose evidence still says unavailable goes on
probation rather than straight to `available`.

### A false alarm

If the usage reading is wrong or a trip was a fluke, force the launchable half:

```bash
aq provider set-state --provider claude --state available --for 1h \
  --reason "usage card is stale; /usage shows 40%"
```

An `available` override always expires. Prefer it to `auto` when the
provider's *own* usage reading is what says exhausted: `auto` resets the
counters, but a usage reading still at or above `usage.exhausted_percent`
trips it again on the next tick. When every provider is down and you know one
is healthy, this is also how you re-admit it.

### Move a task by hand

Naming tasks makes the move explicit. `--to-profile` picks the target (same
class, on an available provider); `--force` also lets it move a **pinned**
task, target a **degraded** provider, or change the class:

```bash
aq provider reroute --task-id demo.42 --to-profile standard-high-claude --dry-run
aq provider reroute --task-id demo.42 --to-profile standard-high-claude --force
```

`--task-id` takes one id or a comma-separated list. A move with `--to-profile`
or `--force` lands in its own `prf-…` batch, is recorded `operator_forced`, and
leaves the task's intent alone — a pinned task moved by hand is still pinned,
now to its new provider. `--force` also skips the trickle and the per-task
limits, and without `--to-profile` lets the sweep pick the target. With neither
flag a named task follows the automatic rules, so a pin still holds. Check
`aq task explain --task-id demo.42` and `aq provider status` first: a task held
`awaiting_failover_capacity` will move on its own within a few sweeps.

A task that is running or claimed is never moved; stop it first.

### Move tasks that were paused before failover

Tasks that were paused on an automatic backoff before provider failover
recorded a cause (no `provider_pause` record) are left alone by default. A dry
run lists them under `skipped` with *use include_paused*. To resume them and
let the sweep move (or hold) them like any other queued task:

```bash
aq provider reroute --include-paused --dry-run
aq provider reroute --include-paused
```

A task an operator paused (no resume time) is never touched by a sweep, even
when named.

## Undo a re-route

Moved work stays moved when the provider comes back: it has a queue position on
a healthy provider, and moving it again is half of a ping-pong. To send it back
anyway:

```bash
aq provider reroute-undo --batch-id prb-codex-7      # every un-undone move of one outage
aq provider reroute-undo --task-id demo.42           # one task (or a comma-separated list)
```

The batch id is in the supervisor notice, in the task's re-route comment and
in `aq provider status --json` (`batch_id`, for the current outage). Undo restores the profile (and the class, if
a forced move changed it), writes an `operator_undo` row and clears
`rerouted_from`. It is refused for a running or claimed task, and while the
original provider is still unavailable unless you add `--force`.

## What reaches Discord

An escalation — one durable incident per outage, delivered to the configured
escalation channel and resolved by AQ itself when the condition clears — is
filed only when a human has to decide something
([escalations](escalations.md)):

| Filed | Not filed |
|---|---|
| A provider is `unauthenticated` (`notify.escalate_unauthenticated`). | `exhausted` with a known reset — there is nothing to decide. |
| A provider has been `failing` for `notify.escalate_failing_after_seconds` (30 min). | `disabled` — an operator chose it. |
| Every session provider is unavailable and none is due back within `notify.escalate_all_down_after_seconds` (30 min), or none has a recovery time. Severity `critical`. | `degraded`, re-route batches, a flapping provider (one inbox message, then quiet), and the `llm` key. |

The incident is filed under the project with the most stranded work. Reply in
its thread to tell the supervisor what you did (for example that you ran
`codex login`); the recovery closes it. Inbox messages (`notify.supervisor`)
and digest facts (`notify.digest`) are separate and can be switched off
independently.

## Recovery: probation, then available

When an outage's end condition is met, the provider becomes `degraded` with
reason `recovering`. At most one launch is admitted — the canary — until one
succeeds; a canary that neither succeeds nor fails within ten minutes is
presumed lost and the next launch may try. The first successful launch makes
the provider `available`, its pools size back up, held work launches at once
and the project default resolves to itself again.

A canary that fails sends the provider straight back to the state it came
from with the backoff doubled (`level + 1`), capped at
`recovery.backoff_max_seconds`. The doubling resets after the provider has
stayed launchable for `recovery.flap_window_seconds`.

## Check the failover playbook is active

Without the `provider-failover` playbook nothing moves: tasks on a dead
provider hold with kind `failover_inactive` and wait for their provider. It is
a required system playbook, activated automatically on the first start.

```bash
aq doctor --check providers.failover_playbook
aq playbook activation-health --playbook-id provider-failover
```

`ready` and enabled is healthy. The daemon's `/ready` endpoint also fails with
a `required_playbooks` entry while it is not. If an operator paused it,
`aq playbook set-enabled --playbook-id provider-failover --enabled` resumes it;
if its artifact no longer validates, `aq doctor --check
playbooks.reviewed_bundles --fix` re-imports the shipped bundle and re-points
the activation. Re-routing is also off — by design, and reported as INFO —
when `provider_failover.mode` is not `enforce` or `reroute.enabled` is false.

## Inputs and outputs

**In:** the providers' own usage readings (the Claude usage probe, Codex
transcript lines), startup dialogs and exit verdicts from sessions, the
daemon's login probe, direct-path call results, your `set-state` overrides,
and the [`provider_failover:`](../reference/configuration.md#provider_failover-keys)
section of `~/.agent-queue/config.yaml`.

**Out:** the effective state per provider, launch suppression, derived holds,
re-routed tasks, `provider.state_changed` / `provider.reroute_batch` /
`task.rerouted` events, inbox messages, escalations and digest facts.

## State ownership

| State | Written by | Lives in |
|---|---|---|
| Provider state, evidence ring, override | The daemon's availability service; overrides by `aq provider set-state` | `provider_availability` table, mirrored in daemon memory |
| Transition log | Availability service, on every effective change | `provider_availability_transitions` (append-only) |
| Task intent | Whoever creates or routes the task | `tasks.provider_intent`, audit in `task_metadata` key `provider_intent_audit` |
| Moves and undos | `provider_reroute` / `provider_reroute_undo` | `task_reroutes`, `tasks.rerouted_from`, a task comment |
| Hand-off note | The daemon, after a session dies on its provider | `task_metadata` key `provider_failover_handoff`, a task comment |
| Holds | Nobody — derived on every read | — |
| Thresholds and policy | Operator | `provider_failover:` in `~/.agent-queue/config.yaml` (hot-reloaded) |

## Common failures and recovery

| Symptom | Diagnose | What to do |
|---|---|---|
| Tasks hold `failover_inactive` and nothing ever moves | `aq doctor --check providers.failover_playbook` | Activate or resume the playbook ([above](#check-the-failover-playbook-is-active)), or check `mode`/`reroute.enabled`. |
| Tasks hold `awaiting_failover_capacity` for a long time | `aq provider held-tasks` (the `ahead` count), `aq pool status` for the target rung | Expected while the target pool works through moved work: the trickle keeps at most `ceil(reroute.target_backlog_factor × max_active)` moved-and-waiting tasks per target rung. Raise that pool's `max_active` if you want more throughput; failover never raises a bound itself. |
| Tasks hold `no_available_target` | `aq provider status` | Every other provider with that class is unavailable or `degraded` — degraded providers are never targets unless `reroute.allow_degraded_target` is true. Wait, or force a move. |
| Tasks hold `no_equivalent_rung` | `aq agent list-profiles` | No other provider runs that class — expected for `astra-*`, which only Codex has — or the task's profile is a role profile, not a worker rung. It waits for its provider. |
| Tasks hold `provider_pinned` | `aq task show` (intent) | A human pinned it. Wait, or [force-move it](#move-a-task-by-hand). |
| Tasks hold `class_policy_hold` or `priority_policy_hold` | `provider_failover.classes`, `reroute.max_priority_value` | Your configured policy says wait. |
| Tasks hold `reroute_limit_reached` | `aq task show` (re-route comments) | Moved automatically `reroute.max_auto_per_task` times, or once within `reroute.task_cooldown_seconds`. A human decides now: force-move or wait. |
| Everything holds `all_providers_unavailable` | `aq doctor --check providers.availability` (ERROR) | Nothing can launch anywhere. Fix a login, or force a provider you know is healthy `available`. |
| `aq doctor` warns `providers.recovery_stuck` | Is the daemon's cycle running? `aq status` | The provider is past its recovery time without entering probation, or logged out with no login probe answering. `aq provider recheck --provider <p>` probes now. |
| `aq doctor` warns `providers.held_tasks` | The check lists hold kinds and ages | Work has been held longer than `provider_failover.doctor.held_warn_seconds` (4 h). Use the rows above for each kind. |
| A task is paused with `needs_attention: provider_failover_push_failed` | `aq task show` | Its session died on its provider and the WIP checkpoint could not be pushed, so it was held rather than moved. Fix the push (network, credentials), then `aq task resume --task-id <id>`; the next slot restores the saved checkpoint. |
| A provider keeps flipping in and out | `aq provider history --provider <p>` | After `notify.flap_threshold` half changes in `recovery.flap_window_seconds` AQ sends one *flapping* message and goes quiet. Consider disabling it with an expiry. |
| `aq provider reroute` answers `disabled` | Its `disabled_reason` | `mode` is `observe`/`off` or `reroute.enabled` is false. The plan is still returned; nothing is applied. |
| The supervisor is refused `provider_held_tasks` or another `provider_*` command | `aq doctor --check profiles.supervisor_capability_drift` | The vault copy of the supervisor profile lacks the grant. The daemon merges shipped supervisor grants on every start and profile reload, so this shows between an upgrade and the next restart, or when the vault copy says `capability_sync: false`. `--fix` merges the missing grants now and keeps every other edit; with the opt-out, add them by hand or run `aq agent profile-reseed --profile-id supervisor --grants-only`. |
| The dashboard shows a provider down that you just fixed | `aq provider status --provider <p> --verbose` | `aq provider recheck --provider <p>` for a login; for anything else, `set-state --state auto` puts it on probation. |

## Related pages

* [Scheduling](../concepts/scheduling.md#provider-availability-and-failover) —
  the rules behind all of this: suppression, pins, the fallback rule, capacity
  protection, the return path.
* [Configuration reference](../reference/configuration.md#provider_failover-keys)
  — every threshold, window and limit named above, with defaults and bounds.
* [Troubleshooting worker sessions](session-troubleshooting.md) — what a
  session that died on a usage-limit or login screen looks like from the
  session side.
* [Escalations](escalations.md) — how the one provider incident reaches Discord
  and how replies work.
* [Providers](../concepts/providers.md) — usage readings, quotas and the direct
  path.

## Source and tests

Implementation: [src/providers/availability.py](../../src/providers/availability.py)
(the state machine), [src/providers/availability_service.py](../../src/providers/availability_service.py)
(evidence, suppression, holds, notifications, escalations),
[src/providers/reroute.py](../../src/providers/reroute.py) (the sweep and
undo), [src/providers/intent.py](../../src/providers/intent.py),
[src/providers/inflight.py](../../src/providers/inflight.py) and
[src/orchestrator/provider_failover.py](../../src/orchestrator/provider_failover.py)
(in-flight failures), [src/commands/provider_commands.py](../../src/commands/provider_commands.py)
(the `aq provider` commands), [src/doctor/provider_availability_checks.py](../../src/doctor/provider_availability_checks.py),
and the playbook [src/prompts/default_playbooks/provider-failover.md](../../src/prompts/default_playbooks/provider-failover.md).
The design record is [docs/specs/provider-failover.md](../specs/provider-failover.md);
where it and the code disagree, the code is right.

```bash
aq test tests/test_provider_availability.py tests/test_provider_evidence.py \
  tests/test_provider_suppression.py tests/test_provider_intent.py \
  tests/test_provider_reroute.py tests/test_provider_inflight.py \
  tests/test_provider_commands.py tests/test_provider_escalation.py \
  tests/test_provider_availability_doctor.py tests/test_usage_limit_screen.py
```
