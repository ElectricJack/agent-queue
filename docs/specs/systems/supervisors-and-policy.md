# System spec: supervisors and policy

Who decides what the factory does, and where those decisions are written down.
This covers:
- the supervisor agents (one per project and one global): how they are named,
  woken, authorized and put back to sleep;
- the duties the supervisor profile gives them;
- the recovery, delivery-watchdog and report paths that send them work;
- durable operator decisions, which hold and release integration work;
- policy profiles, which export and import a project's policy as a file;
- the factory policy page that every agent role follows.

This is system 10 of the 18 in the system inventory. It describes `origin/dev`
at `52d2efe4c` (audited 2026-10-22). Where a guide already explains something
well, this page gives the
short version and links to it. Line numbers are a starting point for a reader;
they drift.

**This system has no code package of its own, and most of it is policy, not
code.** There is no `src/supervisor/`: the old `src/supervisor.py` and its
in-process `Supervisor.chat()` loop were deleted on 2026-08-30 (see
[2026-08-30-llm-direct-path-design.md](../../superpowers/specs/2026-08-30-llm-direct-path-design.md)).
A supervisor today is an ordinary tmux agent session. What makes it a supervisor is:

1. a profile, [src/profiles/defaults/supervisor/profile.md](../../../src/profiles/defaults/supervisor/profile.md);
2. a session name;
3. an elevated API token;
4. the commands that admit that token.

The policy it follows is markdown: the profile, the supervisor-facing rules
in the worker templates, the shipped playbooks, and
[factory-policy.md](../../concepts/factory-policy.md). The code in this system
is small and spread over other packages:
- [src/commands/supervisor_authority.py](../../../src/commands/supervisor_authority.py);
- [src/operator_decisions.py](../../../src/operator_decisions.py);
- [src/policy_profiles/](../../../src/policy_profiles/);
- [src/escalations/supervisor.py](../../../src/escalations/supervisor.py);
- [src/profiles/capability_sync.py](../../../src/profiles/capability_sync.py);
- [src/sessions/supervisor_health.py](../../../src/sessions/supervisor_health.py);
- parts of the session lens, the reconciler and the message queries.

## Contents

- [Purpose](#purpose)
- [Vocabulary](#vocabulary)
- [How it works today](#how-it-works-today)
- [Data model](#data-model)
- [Boundaries](#boundaries)
- [Design principles and invariants](#design-principles-and-invariants)
- [Known gaps](#known-gaps)
- [Planned work](#planned-work)
- [Source and tests](#source-and-tests)

## Purpose

Scheduling, claims and integration in Agent Queue are deterministic and make
no language-model calls. Some work still needs judgment:
- a task failed and someone must choose between retrying it, holding it and
  releasing it;
- a worker asks a question;
- a train stalls;
- the human wants a morning summary;
- an escalation needs an answer.

The rule in [AGENTS.md](../../../AGENTS.md) is *mechanism in code, policy in
playbooks*. This system is where the policy side lives. It has four jobs:

1. **Run the judgment agents.** Each project can have a supervisor session,
   and there is always a global one. They answer questions, plan work through
   change sets, steer stalled work through fenced recovery commands, write
   reports and escalate to the human. They never implement repository changes.
   Workers do that.
2. **Decide who may steer.** One small authority module says which callers may
   run supervisor-grade commands: the local operator, an operator grant, or a
   live elevated supervisor session.
3. **Record the human's decisions durably.** An operator decision is a row
   saying "hold this batch" or "release that hold". Integration commands obey it.
4. **Make policy portable.** A policy profile is a versioned archive of a
   project's playbooks, routing, templates and settings. It can be exported,
   compared and applied to another project or install.

## Vocabulary

| Term | Meaning |
|---|---|
| Project supervisor | The named session `supervisor-<project_id>` (runtime name `n-supervisor--<project_id>`) for one project. |
| Global supervisor | The address `supervisor-global` (runtime `n-supervisor--global`) with no project. Its agent row is ensured on every daemon start; its session starts on demand. It owns cross-project work, reports and operator execution. |
| Elevated token | An API session token minted for a supervisor at cold start. Global elevation is admin scope and loopback-only. |
| Cold start | Launching a sleeping supervisor so it can receive mail (`ensure_started`). |
| Recovery incident | A `supervisor_recovery_incident` record on a task. It holds the task off the frontier until a supervisor retries, holds or releases it. |
| Operator decision | A durable row in `operator_decisions`: a note, hold or release on a task, batch or integration operation. |
| Policy profile | A `aq.policy.v1` archive: a manifest plus one JSON file per item (playbook, routing, template, settings and so on). |
| Factory policy | [docs/concepts/factory-policy.md](../../concepts/factory-policy.md), the normative page for admitting, delivering and recovering work. |
| Change set | A proposed batch of task edits (`aq task batch-propose`) that does nothing until a human decision applies it. |

## How it works today

### The big picture

```
 events / failures / questions                 human (CLI, dashboard, Discord)
            │                                              │
            ▼                                              ▼
  playbooks (supervisor-failure-triage,         operator decisions, reviews,
  morning-report, …) and daemon sweeps          supervisor-inbox replies
            │                                              │
            └──────────► messages to supervisor-<pid> ◄────┘
                         or supervisor-global
                                   │  (wake on delivery)
                                   ▼
                    tmux session running the supervisor profile
                                   │  elevated token
                                   ▼
             CommandHandler: task recover, batch-propose, report submit,
             decision record, integration controls, cron register, …
```

Nothing in this picture runs a model inside the daemon. The playbooks named
here are deterministic: `supervisor-failure-triage` calls one command and
`morning-report` calls one tick command. The model runs only in the supervisor's
tmux session. That session runs a harness CLI, just as a worker session does.

### The supervisor profile

The shipped profile is
[src/profiles/defaults/supervisor/profile.md](../../../src/profiles/defaults/supervisor/profile.md):
- harness `claude`, default class `deep-high`;
- lifecycle `named`, `wake_mode: resume`, `idle_timeout: 2700` seconds;
- no workspace.

It defines four duties:

- **Answer.** Reply to worker questions and human messages.
- **Plan.** File work with routing *hints*, never routes. Larger edits go
  through change sets, described in [task-change-sets.md](../../guides/task-change-sets.md).
- **Steer.** Act on recovery notices with `aq task recover --decision
  retry|hold|release`, read `aq integration status` and the doctor checks, and
  run a stall patrol.
- **Escalate.** Bring the human in through `aq escalation apply-reply`,
  reviews and reports. Escalate promptly: a supervisor must never leave work
  parked on an indefinite recovery hold.

Rules worth knowing before reading the profile:
- it never routes work to itself;
- it uses `aq prime` for its session scope, never as a queued task worker;
- only the human decides a review unless the human delegates it;
- "the supervisor performs vault and control-plane changes, workers implement
  repository changes" (from the 2026-09-11 factory policy simplification).

The worker templates (`src/profiles/defaults/worker-*/profile.md`) carry the
matching rule from the other side: when scope is unclear, a worker records the
blocker and asks the supervisor rather than guessing.

**Stall patrol.** On a cold start the profile tells the supervisor to register
one patrol:

```
aq cron register --every 900 --offset 120 --idempotency-key supervisor-patrol-v1 \
  --prompt 'Run ~/.agent-queue/operator-checks/stall-sweep.py and handle its findings.'
```

The patrol runs `~/.agent-queue/operator-checks/stall-sweep.py`, installed from
[scripts/operator-checks/stall-sweep.py](../../../scripts/operator-checks/stall-sweep.py).
The scheduling mechanism is agent cron ([src/agent_cron.py](../../../src/agent_cron.py),
[src/commands/cron_commands.py](../../../src/commands/cron_commands.py)). The
daemon does not register the patrol. See [behaviour gaps](#behaviour-gaps).

**Seeding and drift.** `ensure_supervisor_profile` in
[src/vault.py](../../../src/vault.py) writes
`vault/agent-types/supervisor/profile.md` only if it is absent, like every
other shipped profile. An operator's edits are therefore never overwritten. The
exception is the `## Capabilities` section.
[src/profiles/capability_sync.py](../../../src/profiles/capability_sync.py)
merges any shipped grants that the vault copy lacks. It runs on every daemon
start and every profile reload, and it is additive: it never removes a grant.
It writes atomically and keeps a `.bak-<epoch>` copy. Frontmatter
`capability_sync: false` opts a profile out. The doctor check
`profiles.supervisor_capability_drift`
([src/doctor/profile_checks.py](../../../src/doctor/profile_checks.py)) reports
the drift and has a `--fix`. Profile structure and inheritance in general belong
to [profiles.md](../design/profiles.md).

### Names, lifecycle and waking

Every supervisor is on demand. There is no always-on supervisor loop.

- **Names.** `supervisor-<pid>` maps to the runtime name `n-supervisor--<pid>`
  (`named_session_name` in [src/sessions/spec.py](../../../src/sessions/spec.py);
  the mapping in [src/messages/session_lens.py](../../../src/messages/session_lens.py)).
  The global supervisor's agent row is defined in
  [src/agents/configuration.py](../../../src/agents/configuration.py), and the
  daemon creates it on every start (`src/orchestrator/core.py`).
- **Per-project switch.** `projects.supervisor_enabled` defaults to false.
  Disabling a project's supervisor:
  - sets its desired state to stopped;
  - redirects its mail to `supervisor-global`
    ([src/database/queries/project_queries.py](../../../src/database/queries/project_queries.py));
  - makes the reconciler drain a running session with end reason
    `project_supervisor_disabled`.

  The doctor check `sessions.disabled_project_supervisors` reports what is left
  over.
- **Cold start.** `ensure_started` in `session_lens.py` does
  the following:
  1. takes a per-name lock;
  2. refuses a disabled project;
  3. sets the working directory to `vault/projects/<pid>`, or the vault root
     for the global supervisor;
  4. mints the elevated token;
  5. resumes the previous harness session key, but only when `wake_mode` is
     `resume` and the harness supports it;
  6. writes a session row with lifecycle `named`.

  If the launch fails, it revokes the token.
- **What wakes a supervisor.** A supervisor wakes when:
  - the message delivery engine finds the recipient sleeping
    ([src/messages/delivery.py](../../../src/messages/delivery.py));
  - the session lens's absent-recipient signal fires;
  - the reconciler's safety net `_converge_named_up` runs (with backoff and
    quarantine);
  - an operator runs `aq session wake`.

  Scheduled prompts, conversation-only batches, ask-input batches and task
  notifications deliberately do not wake it.
- **Sleep.** The reconciler drains an idle supervisor to sleeping with reason
  `idle_timeout`. It skips this while the supervisor still has supervision work
  (`has_supervision_work`, `src/sessions/reconciler.py`). Turn-health recovery
  runs before idle handling; terminal output alone does not prove health.
- **Restart.** `aq supervisor restart`
  ([src/commands/session_commands.py](../../../src/commands/session_commands.py)
  `_cmd_supervisor_restart`; `restart_supervisor` in `session_lens.py`) admits
  the local operator, the live global supervisor, or the target project's live
  supervisor. Other projects, ordinary workers and stale launch identities are
  refused. It defaults to `supervisor-global`, and resume defaults to off.
  CLI `--session-id` (command argument `session_id`) fences the expected current
  session; the lens calls this `expected_session_id`. It ends the old session
  with `operator_restart`, revokes the token and relaunches. On a daemon
  restart, `adopt_on_start` in the reconciler re-adopts live `n-` sessions by
  instance token, so a supervisor survives `aq restart`.

Session mechanics (tmux, harnesses, resume keys, the reconciler itself) belong
to `sessions-and-harnesses`. This page covers only what is special about
supervisors.

### Turn health and recovery

[supervisor-turn-health.md](../supervisor-turn-health.md) describes the health
mechanism shipped on 2026-10-21. It is separate from undelivered-message alerts:

- The reconciler reads a bounded tail of the exact Codex or Claude conversation,
  scoped to the current launch. It records completed turns, provider errors,
  unanswered requests and retry clocks in `sessions.supervisor_health`. Missing
  or partial transcript evidence is unknown; a busy pane is not a completed turn.
- An unanswered turn times out after `supervisor.turn_timeout_seconds` (900).
  Provider capacity, rate-limit and authentication errors also trigger recovery.
  The default `max_turn_retries` is two nudges, with exponential backoff starting
  at 60 seconds and capped at 1800, followed by a fresh restart. Only a successful
  turn clears the failure budget. Restarting the daemon or conversation does not.
- Replacement uses the serialized, instance-fenced stop/revoke/relaunch path.
  An uncertain stop prevents replacement. Recovery may choose an available
  installed harness in the same intelligence class under provider failover
  policy; explicit model pins remain binding. It never rewrites the profile.
- Unanswered worker requests escalate once after `question_timeout_seconds`
  (900), including consumed requests and requests to prior instance UUIDs.
  Project requests go to the global supervisor; global requests go to the
  dashboard. The notice preserves the original thread and request pointer.
  It does not answer the worker or satisfy its verified message wait.
- A known idle completed turn rotates fresh after `max_session_age_seconds`
  (86400) or `context_ceiling_tokens` (160000 input tokens). Active or unknown
  turns do not authorize rotation. Recovery writes a durable handoff with the
  prior conversation and pending request IDs.

Session list/show and the dashboard expose health samples. The read-only stall
sweep reports `SUPERVISOR-UNHEALTHY` or `SUPERVISOR-UNKNOWN`; it does not repair
sessions.

### Messages to a supervisor

Supervisor addressing is resolved at three points.

- **Write time.** `_supervisor_recipient` in
  [src/database/queries/message_queries.py](../../../src/database/queries/message_queries.py)
  recognizes any of these recipients:
  - `profile:supervisor`;
  - `session:supervisor-*` or `n-supervisor--*`;
  - a supervisor's UUID.

  A message to a disabled project's supervisor is rewritten to
  `session:supervisor-global`. `redirect_disabled_supervisor_messages` repeats
  that redirect on every delivery pass and before every inbox read.
- **Delivery time.** The session lens resolves the recipient:
  - a project suffix is authoritative;
  - a non-project suffix aliases to the message's own project supervisor;
  - anything else is undeliverable.

  After 24 hours an undeliverable message is parked back to the sender, with a
  `[parked]` notice to the humans.
- **Send time.** A supervisor address that names no project is an error. A
  project supervisor may message `supervisor-global`.

Native `aq agent message` guidance from a live project supervisor is signed
`session:supervisor-<project>` and supports `aq message reply`. Operator,
automation and global-supervisor guidance uses `system:supervisor`, which cannot
be replied to; answer through an explicit supervisor mailbox instead. Ordinary
`aq message send` derives the authenticated sender session UUID. When answering
a worker request with `aq agent message`, include `--reply-to <request-id>` so
the reply joins the thread its durable message wait verifies. Delivery or an
unthreaded notice does not satisfy that wait. The general message model, inbox and nudges
belong to `messaging-and-human-asks`; see
[messaging.md](../../concepts/messaging.md).

### Authority

[src/commands/supervisor_authority.py](../../../src/commands/supervisor_authority.py)
has three gates:

| Gate | Admits | Used by |
|---|---|---|
| `operator_or_supervisor` | Loopback operator; an operator grant; an elevated, live, named session with profile `supervisor` (state starting, running or draining, desired running) of the same project or global | `decision_record` / `decision_list`, routing commands, several task commands, project commands, onboarding |
| `integration_operator` | The same, under the integration name | About 40 integration commands, promotion, superseded completion, one session command |
| `operator_or_global_supervisor` | Loopback operator, or the global supervisor whose `instance_token` matches | `aq task recover --operator-release`, workspace checkout repair, project repository checkout repair |

The token side is in [src/api/scope.py](../../../src/api/scope.py): global
elevation is admin and loopback-only. Elevated tokens are minted only at cold
start or by an explicit re-mint (`session_commands.py`), and renewal is in
`api_session_token_queries.py`. The doctor check
`uncredentialed_supervisor_sessions`
([src/doctor/session_checks.py](../../../src/doctor/session_checks.py)) finds a
supervisor without a token. Trust levels in general are described in
[trust-and-ops.md](../design/trust-and-ops.md).

### Recovery: the supervisor's main loop of work

When a task needs a judgment call, the daemon parks it under a recovery
incident instead of guessing. Reasons include a worker failure, a slot reset
that failed, or a stuck repair. The code is in
[src/database/queries/task_recovery_queries.py](../../../src/database/queries/task_recovery_queries.py).

1. `queue_task_recovery_notifications` runs every tick. For each new incident
   it sends message `msg-<incident_id>` to `supervisor-<pid>`:
   - subject `Task recovery: <task_id>`;
   - body kind `task_recovery`;
   - priority 60.

   A recovery owned by an integration operation is `not_actionable` and is not
   sent.
2. The frontier excludes the task while the incident is open
   (`recovery_hold` in
   [claim_queries.py](../../../src/database/queries/claim_queries.py)). The
   frontier also never offers a task whose profile is `supervisor`.
3. The supervisor answers with `aq task recover --decision`
   (`task_commands.py`):
   - **retry** checks that the incident is still the current one;
   - **hold** keeps the task parked;
   - **release** needs `--operator-release --expected-hold-at` and the
     `operator_or_global_supervisor` gate.

   Tasks owned by an integration operation are refused.
4. Some holds have a deadline: `slot_reset_failed`, train repair and live batch
   repair stages (`_recovery_hold_deadline`). When one expires,
   `_escalate_expired_recovery_hold` sends one notice to both the project and
   the global supervisor (the profile says "after 15 minutes"). The hold itself
   stays: the barrier is preserved until someone decides.

The `supervisor-failure-triage` playbook connects task failure to this path.
It triggers on `task.failed` and calls one command, `task_failure_triage_notify`,
which calls `notify_task_recovery`. It has no retry logic of its own: the
retry, hold or release decision belongs to the supervisor.

### The delivery watchdog

[src/escalations/supervisor.py](../../../src/escalations/supervisor.py)
(`SupervisorDeliveryWatchdog`) watches messages to `supervisor-%` whose body
kind is `agent_question`, `task_recovery` or `escalation_reply`. If one stays
undelivered longer than `supervisor_delivery_timeout_minutes` (default 15), it
raises a low-severity `supervisor_delivery` incident, which resolves itself once
the message is delivered. The watchdog only reports. It never approves or
decides anything. Every escalation row names a `supervisor_owner` (NOT NULL),
so every incident has a supervisor to answer it. Escalation intake, rendering
and the human channel belong to `messaging-and-human-asks`; see
[escalations.md](../../guides/escalations.md).

The approved RCA
`2026-10-07-missed-supervisor-message-wake-up-rca-and-bounded-delivery-f.md`
(review `rev-keen-current`) explains why this exists. A supervisor missed a
message because the tmux composer safety guard refused the wake. Three
delivery defects were fixed in `10bae4acf`.

### Reports and the digest

Only the global supervisor writes reports.

- `aq report brief` and `aq report submit`
  ([src/commands/report_commands.py](../../../src/commands/report_commands.py))
  accept only the live `n-supervisor--global` with a matching token. A project
  supervisor is refused. Hourly `submit` rejects URLs, shipment claims without
  a `delivery:` reference and rendered output over 1,200 characters (including
  the daemon's footer and marker). Morning requests instead accept version 1
  JSON (`summary` and per-project evidence/manual checks), bounded at 32 KiB;
  the daemon owns coverage, provenance and delivery links. Both require the
  current brief hash/version and scoped evidence before the author deadline.
- A report request is message `msg-<request_id>` from `reports` to
  `supervisor-global`, priority 100 (`report_queries.py`). If the supervisor
  does not answer, the morning report posts a fallback
  (`finalize_morning_fallback`), and the hourly report posts its fallback at
  `due_at`.
- The `morning-report` playbook triggers on `report.morning_due`, which
  [src/reports/morning_due.py](../../../src/reports/morning_due.py) emits. It
  calls `morning_report_tick`, waits on durable 60-second timers while that day's
  report remains pending, and retries a failed call once. It makes no model
  call and ships disabled. `reports.morning.enabled` and an explicit activation
  enable scheduling; full-fleet visibility is also required for authoring.
- Hourly reports are opt-in and need configuration plus a manual import of the
  reviewed playbook. See [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md).
- `aq digest facts` and `aq digest post` let the supervisor write the Discord
  digest window when `discord.digest.supervisor_authored` is on (default off).

### Discord: the supervisor inbox

Discord is a chat extension of the supervisor
(`2026-10-03-discord-as-a-chat-extension-of-the-supervisor.md`, review
`rev-brisk-flare`, approved). The commands are in
[src/commands/conversation_commands.py](../../../src/commands/conversation_commands.py):
- `supervisor_inbox_post` is accepted only from the discord-gateway service;
- status and history are read-only;
- `supervisor_inbox_reply` is accepted only from the exact live supervisor
  launch.

Conversations are opt-in (`discord.conversation.enabled`). See
[discord-conversations.md](../../guides/discord-conversations.md).

### Operator decisions

An operator decision is the human's durable hold or release on integration
work. The design is
[operator-decisions.md](../design/operator-decisions.md); the code is
[src/operator_decisions.py](../../../src/operator_decisions.py).

- `OperatorDecisions.record` locks the project row (`FOR UPDATE`). It then
  checks two things:
  - an idempotent replay must match the original exactly;
  - a release must name an active hold.
- `related_on` widens a decision on a batch to its member tasks and repair
  operations, so a hold on a batch holds its pieces.
- `control_refusal` refuses `integration_*` and `delivery_promote` commands
  while a hold applies. Read-only commands are exempt. The outcome is
  `operator_decision_hold`, or `unauthorized` for a cross-project attempt. The
  check runs centrally in `CommandHandler`
  ([src/commands/handler.py](../../../src/commands/handler.py)).
- The commands are `aq decision record` and `aq decision list`
  ([src/commands/decision_commands.py](../../../src/commands/decision_commands.py);
  contract in `src/commands/contracts/decisions.py`). Both use the
  `operator_or_supervisor` gate. `decision_record` is in `OPERATOR_FORBIDDEN`
  ([src/commands/operator_authority.py](../../../src/commands/operator_authority.py)),
  so an operator grant cannot record decisions on the human's behalf.

Batch and train code also reads holds directly (see [known gaps](#known-gaps)).

### Policy profiles

A policy profile is a project's policy as a file. The design is
[policy-profiles.md](../design/policy-profiles.md); the code is
[src/policy_profiles/](../../../src/policy_profiles/) and the commands are in
[src/commands/policy_commands.py](../../../src/commands/policy_commands.py).

- **Format** `aq.policy.v1`: `manifest.json` plus `items/NNNN.json`, each item
  with a sha256 checksum. A path ending `.zip` or `.aqpolicy` is a zip archive,
  written create-only. Any other path is a directory. Limits are 32 MiB and
  1,000 items, with no symlinks.
- **Item types:** playbook, agent settings, promotion flow, CI test, routing,
  template, intelligence class, pool, project settings, integration settings.
- **Built-in default** (`builtin.default_policy()`):
  - the `default-pipeline` and `supervisor-failure-triage` playbooks at project
    scope;
  - `default-assignment-routing` at system scope;
  - routing preferences;
  - the spec and plan templates.

  `aq project create --from-policy default|<path>|none` applies it
  ([src/cli/projects.py](../../../src/cli/projects.py)).
- **Portability** ([integration.py](../../../src/policy_profiles/integration.py)):
  - export strips install-local values;
  - the portable form replaces the repository, checks and commands with
    `{{placeholders}}`;
  - import refuses an integration engine or branch mismatch and requires local
    routes.
- **Commands** `aq policy export|diff|apply` ([src/cli/policy.py](../../../src/cli/policy.py)):
  - `export` writes with a checksum compare-and-swap;
  - `diff` reports each item as new, identical or "will overwrite", with the
    current checksum;
  - `apply` runs under one lock. It applies integration settings through
    `edit_project` and writes the files. It syncs profiles to the database. It
    then submits each playbook for review and never activates one, recording a
    `review.json` receipt under `policy/pending-playbooks/<id>/`.

  The CLI selects items with `--only`, `--skip`, `--scope ITEM=project|global|skip`
  and `--no-overwrite`, and fills placeholders with `--set NAME=VALUE`.
  A system-origin item needs an explicit scope choice. `--yes` accepts project
  defaults, skips system-origin items without `--scope`, and never authorizes
  overwrites. `diff` supports `--json`; export, apply and project creation with
  an applied policy reject JSON mode before starting their interactive flow.
  Omitting `--from-policy` asks on an interactive terminal; in scripts/JSON it
  means no policy. Cancelling import leaves the newly created project in place.
- **Authority.** `_policy_authority` admits the local operator or an operator
  grant, not a supervisor token. An operator grant may import at project scope
  only and cannot edit agents.

There is no database table for policy profiles. A profile is a file, and an
applied profile leaves only its effects (files, settings, pending reviews).

### Factory policy

[docs/concepts/factory-policy.md](../../concepts/factory-policy.md) is the
normative policy for every agent role. It has three sections: admit work (A),
deliver work (B) and recover work (C).
[tests/test_factory_policy_instructions.py](../../../tests/test_factory_policy_instructions.py)
is its ratchet. The test fails if:
- a retired instruction pattern reappears in any shipped prompt;
- a policy role (the supervisor among them) loses its section;
- a shipped route goes missing;
- any instruction says `gh pr merge`.

It warns if the page grows past 90 lines. Its source is the user-authorized
`software-factory-policy-simplification-2026-09-11.md` in the vault, which has
no review.

### Playbooks this system relies on

Playbook mechanics belong to the `playbooks` system. The ones that carry
supervisor policy are in
[src/prompts/default_playbooks/](../../../src/prompts/default_playbooks/), with
reviewed bundles in
[src/prompts/reviewed_playbooks/](../../../src/prompts/reviewed_playbooks/):

| Playbook | Trigger | Installed |
|---|---|---|
| `supervisor-failure-triage` | `task.failed` | Activated on first install (`DEFAULT_SYSTEM_PLAYBOOK_IDS` in [src/playbooks/required.py](../../../src/playbooks/required.py)) and in the built-in policy profile |
| `morning-report` | `report.morning_due` | Installed disabled on first install |
| `supervisor-hourly-report`, `supervisor-digest`, `blocked-task-escalation` | Timers and events | Reviewed bundles only; an operator imports and activates them |
| `stuck-task-watch` | Timer | Available as a reviewed bundle for explicit installation; absent from the first-install inventory |

The playbooks `default-assignment-routing`, `provider-usage-probe` and
`provider-failover` are *required* for readiness. Ordinary startup observes
durable activations without restoring deleted policy or changing operator
choices. An external first-install receipt permits one initial installation
and activation attempt; deletion of the vault does not renew it. Explicit
`aq doctor --check playbooks.required --fix` repairs required activations.
The defaults are installed once and can be disabled; optional bundles require
deliberate import and activation. See [policy-profiles.md](../design/policy-profiles.md).

## Data model

All tables are in [src/database/tables.py](../../../src/database/tables.py).

| Table | Columns this system reads or writes |
|---|---|
| `operator_decisions` | `id`, `project_id`, `object_kind` (task, batch or operation), `object_id`, `effect` (note, hold or release), `operator`, `decision`, `source` (chat, discord or cli), `source_ref`, `recorded_by`, `created_at`, `idempotency_key`, `releases`. Named check constraints; unique `(project_id, idempotency_key)`; unique `releases`, so a hold is released at most once. |
| `projects` | `supervisor_enabled` (default false) |
| `sessions` | Named supervisor rows: `profile_id = 'supervisor'`, lifecycle `named`, `state`, `desired_state`, `end_reason` (`idle_timeout`, `operator_restart`, `project_supervisor_disabled`, `provider_error`, `turn_timeout`, `session_rotation`), `instance_token`, resume key and persisted `supervisor_health` |
| `api_session_tokens` | The elevated token per supervisor launch |
| `task_metadata` | `supervisor_recovery_incident` (the recovery hold, 900 s hold window) |
| `messages` | Recovery notices, report requests, questions and replies addressed to `supervisor-*` |
| escalation tables | `supervisor_owner` (NOT NULL) and the `supervisor_delivery` incident kind |

Policy profiles have no table; see [above](#policy-profiles). Agent cron
registrations (the stall patrol) belong to the agent-cron tables.

Configuration lives in `~/.agent-queue/config.yaml`; see
[config.md](../config.md).

| Key | Meaning |
|---|---|
| `discord.escalation.supervisor_delivery_timeout_minutes` | Watchdog threshold, 1–1440, default 15 |
| `discord.conversation.*` | Opt-in Discord conversations with the addressed supervisor |
| `discord.digest.supervisor_authored` | The supervisor writes the digest window (default false) |
| `reports.hourly.*` | Opt-in hourly reports |
| `reports.morning.*` | Opt-in morning schedule and authoring visibility |
| `supervisor.turn_timeout_seconds` | Unanswered turn threshold; default 900 |
| `supervisor.question_timeout_seconds` | Once-only unanswered worker-request escalation; default 900 |
| `supervisor.retry_backoff_seconds`, `supervisor.max_turn_retries` | Initial retry backoff 60 seconds, two nudges before fresh restart; backoff capped at 1800 seconds |
| `supervisor.max_session_age_seconds`, `supervisor.context_ceiling_tokens` | Rotate known idle turns at 86400 seconds or 160000 input tokens |
| `supervisor.global.idle_timeout_seconds` | Default 2700. Only logged; see [known gaps](#known-gaps). |
| `supervisor_agent.enabled`, `supervisor_agent.idle_timeout` | Validated only; see [known gaps](#known-gaps). |

The supervisor's real idle timeout and wake mode come from the profile
frontmatter, not from config.

## Boundaries

| Neighbour | What it owns | Where it meets this system |
|---|---|---|
| `daemon-core` | `run_one_cycle` ordering, the event bus, startup | Ordering the per-tick recovery notifications, the watchdog, report due events and capability sync is daemon-core's job. This system owns what those steps do. Startup creates `supervisor-global`. |
| `task-graph` | Status, hierarchy, task edits | Supervisors file and reshape work through task commands and change sets. |
| `scheduling-and-claims` | The frontier, pools, claims | The frontier never offers a `supervisor` profile task and excludes tasks under a recovery hold. Workers report repeated claim refusals to the project supervisor. |
| `routing-and-profiles` | Profile parsing, derived rungs, routing | The supervisor profile is one profile among many. Supervisors file with hints and never route. |
| `sessions-and-harnesses` | tmux sessions, named lifecycle, resume, reconciler, adoption | Supervisors are named sessions. Cold start, wake, sleep and restart use its machinery. |
| `projects-workspaces-git` | Projects, workspaces | `projects.supervisor_enabled`. Workspace checkout repair is gated on the global supervisor. Supervisors need no workspace. |
| `integration-and-delivery` | Trains, batches, repairs, promotion | `integration_operator` gates its controls. Operator decisions hold its batches and trains. Train and repair recovery holds escalate to supervisors. |
| `playbooks` | The V2 runner, required playbooks, reviewed bundles | Supervisor policy ships as playbooks. A policy profile import submits playbooks for review. |
| `messaging-and-human-asks` | Messages, delivery, escalations, Discord | Supervisor address resolution, the delivery watchdog and the supervisor inbox sit on its transport. |
| `reviews-and-knowledge` | Document reviews | Only the human decides a review unless the human delegates it. Policy imports create pending playbook reviews. |
| `providers` | Availability and failover | Provider-hold reasons are what a supervisor reads during an outage. Failover policy is a required playbook. |
| `configuration-and-vault` | Config loading, vault seeding | Write-if-absent seeding of the supervisor profile; capability sync. |
| `api-and-clients` | Token scopes, routes | Elevated-token scope rules (`src/api/scope.py`) and `POST /api/sessions/{name}/message`. |
| `dashboard` | Operator UI | Shows supervisor sessions, escalations and decisions. |
| `operations-and-recovery` | doctor, restart, runbooks | The doctor checks named in this page; `aq supervisor restart`; supervisors survive `aq restart` by adoption. |
| `plugins-mcp-and-tools` | MCP surface, aq skills | Supervisors reach commands through the same CLI and MCP surface as other agents. |
| `test-service-and-jobs` | `aq test` | Not used: supervisors do not run tests. |

## Design principles and invariants

These hold in the code today. A change that breaks one needs a spec.

1. **Supervisor judgment runs outside the daemon.** It happens inside a
   supervisor's tmux session. The playbooks that feed it are deterministic
   (commands and durable timers). Other systems use the daemon's direct LLM
   path; this is not a ban on all daemon LLM calls.
2. **The supervisor is a profile, not a class.** Behaviour changes go in the
   profile, the worker template, a playbook or factory policy. A new Python
   "supervisor loop" would undo the 2026-08-30 removal.
3. **Supervisors steer; workers implement.** A supervisor never claims or
   implements repository work, and the frontier refuses `supervisor` profile
   tasks.
4. **One authority module.** Supervisor-grade commands use the gates in
   `supervisor_authority.py`. An elevated token is valid only for a live,
   named, supervisor-profile session of the same project, or the global one.
5. **Recovery is fenced and explicit.** A held task leaves the frontier until
   someone decides. Release needs the hold timestamp and the global supervisor
   or operator. An expired deadline escalates but never lifts the hold.
6. **Human decisions are durable rows.** A hold is recorded once,
   idempotently, released at most once, and widened from a batch to its
   members.
7. **Shipped policy never overwrites operator policy.** Profiles and skills are
   write-if-absent. Capability sync only adds grants. A policy import never
   activates a playbook: it submits each one for review.
8. **Every escalation has an owner.** `supervisor_owner` is NOT NULL, and an
   undeliverable supervisor message is surfaced, not dropped.

## Known gaps

### Historical material

The old in-process class in [supervisor.md](../supervisor.md), the August
[design](../design/supervisor-agent.md) and
[implementation](../implementation/supervisor-agent.md) are historical records.
Use this system page and [supervisor-turn-health.md](../supervisor-turn-health.md)
for current behavior. `src/prompts/supervisor_system.md` is also a retained
legacy prompt: only PromptBuilder tests select its identity today. The live
supervisor instructions come from the shipped/installed profile.

### Stale code and comments

- **Legacy settings do not control supervisor lifecycle.**
  - The profile key `mode` (`on_demand`) is parsed but never read.
  - `supervisor.global.idle_timeout_seconds` is only logged at cold start.
  - `supervisor_agent.enabled` and `supervisor_agent.idle_timeout` are only
    validated.

  The idle timeout that takes effect is the profile's `idle_timeout`.
- **`control_refusal` is not the only place that reads holds.**
  The central check in `handler.py` is its only call site. But
  [src/integration/batches.py](../../../src/integration/batches.py) and
  [src/integration/train_sources.py](../../../src/integration/train_sources.py)
  read operator holds directly, so a change to hold semantics must update all
  three.

### Behaviour gaps

- **The stall patrol depends on the supervisor.** The 15-minute patrol exists
  only if the supervisor registered it with `aq cron register` after a cold
  start. Nothing in the daemon checks that it is registered. Operational policy
  that relies on this patrol therefore needs verified registration.
- **Policy profiles have no applied state.** No record says which profile a
  project was created from or last applied, and nothing keeps a project in sync
  with a profile. `diff` compares against current files and settings.
- **Supervisor delivery has failed before.** The RCA above fixed three defects.
  The delivery watchdog reports a missed delivery after 15 minutes; it does not
  repair a composer that refuses input. The separate turn-health mechanism
  described above can recover a timed-out or failed supervisor turn.
- **Report authorship is global only.** Project supervisors cannot submit
  reports, so per-project narrative goes through the global supervisor.

## Planned work

This is a snapshot of open tasks, re-queried on 2026-10-20. Vault documents are
under `projects/agent-queue/specs/` in the operator vault and are cited by file
name, with the review id and state at the time of writing.

The following roadmap and review states are **historical evidence from that
date**, not current task status or instructions to perform operator cutovers.
In particular, turn-health recovery and same-project supervisor restart shipped
after this snapshot, and verified waits are enabled in the audited checkout.
Current behavior above is verified against repository code; live task and
review state requires a fresh authorized read.

### Historical epics and tasks (2026-10-20)

**`stark-willow-95`, fast and smooth integration** (IN_PROGRESS). It implements
`2026-10-12-fast-smooth-integration.md` (review `rev-fair-lantern`, revision 2
approved 2026-10-14; the vault frontmatter's "revision 3, in review" is stale)
and the companion `2026-10-13-fast-smooth-integration-revision-2.md`. Three
decision notes sit beside it with no review:
- `2026-10-13-fast-smooth-integration-rev2-decisions.md`;
- `2026-10-13-fast-smooth-integration-rev3-playbook-policy.md`: train policy
  lives in a per-project playbook, which is also the on/off switch; "green
  wins"; default to proceed; keep human gates small;
- `2026-10-20-fast-smooth-integration-rev4-retrospective-decisions.md`: fixers
  work in place, git is the truth, and "the supervisor watches it every
  patrol" and hand-merges stalls.

| Task | State | What it changes here |
|---|---|---|
| `.17` Align shipped policy, profiles, instructions and retirement ratchets | DEFINED | Rewrites factory policy, worker templates, prime, aq skills and reviewed bundles to match the new integration model. Adds a ratchet that no active receipt carries authority. Grants only scoped close-candidate and fixer permissions. Keeps capability sync additive and reports installed drift without overwriting. Acceptance: `aq test tests/test_delivery_receipt_retirement.py tests/test_factory_policy_instructions.py tests/test_v1_removal.py`. |
| `.18` Validate the complete flow and hand operators the cutover manifests | DEFINED | End-to-end acceptance, including a supervisor-only budget escalation, plus operator checklists. No live activation. |
| `.3` Redrive stuck delivery once | READY | A delivery watchdog and doctor stall checks, with a reviewed V2 policy bundle owning recovery decisions and blockers recorded as wait records (`aq wait explain`). Consumes the supervisor escalation from `bold-zenith-11.12`. |
| `.10` Make epic-level checks advisory | DEFINED | Changes what a supervisor must chase on a parent: required checks become advisory by default. |

**`clear-impact-72`, playbook building blocks** (IN_PROGRESS, requested by
the human on 2026-10-20). `.11` (READY) evaluates rewriting
`supervisor-failure-triage` onto the reusable building blocks, and `.5` (READY)
does the same for `morning-report`. Each would write a plan to the vault's
`plans/` folder. Their prerequisites are complete. The building blocks come
from `2026-10-12-implementation-playbooks-reusable-building-blocks.md`
(`rev-smart-flare`, approved).

**`calm-grove-14`, H09: remove duplicate human publishers** (READY). Migration
93 renames the escalation message and action tables to ask correspondence and
ask operations. It deletes `reviews/notifier.py`, removes the human paths from
the escalation dispatch, plan, render, sweep and autoresolve modules, and
updates the supervisor and worker templates and the aq-reviews and aq-comms
skills. This changes the tables `supervisor_owner` lives in and the supervisor
profile's escalation instructions.

**`vivid-rapids-36` (H09-OPS, READY) and `vivid-pinnacle-60` (H08-OPS,
PAUSED)** are operator cutover tasks from the one-place-to-ask-jack work (parent
`stark-forge-10`, COMPLETED). A 2026-10-19 message from `supervisor-global`
settled that the global supervisor carries out operator execution, while the
human keeps review decisions.

**`nimble-lantern-37`, held ingestion P5** (IN_PROGRESS). `.2` (READY)
changes the spec-ingest profile text and the `default-pipeline` rules, which
the built-in policy profile ships. `.3` (DEFINED) updates the docs. Source:
`2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md`
(`rev-agile-beacon`, approved).

**`bold-zenith-11`, hibernation and verified waits.** `.22` (READY) flips the
new defaults on and ships the matching doctor checks and grant sync. The
completed `.12` added the supervisor escalation and loop detection that
`stark-willow-95.3` consumes. Source:
`2026-10-13-event-driven-agent-hibernation-and-verified-waits-implementa.md`
(`rev-fleet-orbit`, approved).

**`agile-vault-38`** (BLOCKED) would refuse playbook scripts by default. It
conflicts with `2026-10-14-playbook-scripts-no-sandbox-decision.md`, a
decision note with no review.

### Historical review snapshot (2026-10-20)

| Document | Review and state | What it means for this system |
|---|---|---|
| `2026-10-11-deliberate-playbook-installation-and-project-policy.md` | `rev-fresh-journey`, approved (revision 1) | The source of policy profiles and first-install-only playbooks. Implemented. |
| `2026-10-11-one-place-to-ask-jack-cohesive-human-communication-in-aq.md` | `rev-fair-ridge`, approved (revision 3) | One path for asking the human. It moves human publishing out of escalations; `calm-grove-14` is the remaining removal. |
| `2026-10-11-one-place-to-ask-jack-implementation-specification.md` | `rev-sharp-zenith`, approved (revision 1) | Implementation of the above, including the H08/H09 operator tasks. |
| `2026-10-12-fast-smooth-integration.md` | `rev-fair-lantern`, approved (revision 2); frontmatter stale | Puts train policy in playbooks and gives the supervisor patrol a larger role. |
| `2026-10-12-implementation-playbooks-reusable-building-blocks.md` | `rev-smart-flare`, approved (revision 1) | The building blocks `clear-impact-72` would rewrite the supervisor playbooks onto. |
| `2026-10-13-event-driven-agent-hibernation-and-verified-waits-implementa.md` | `rev-fleet-orbit`, approved (revision 1) | Verified waits and supervisor escalation of loops. |
| `2026-10-07-missed-supervisor-message-wake-up-rca-and-bounded-delivery-f.md` | `rev-keen-current`, approved (revision 1) | The delivery fixes behind the watchdog. Done (`10bae4acf`). |
| `2026-10-03-discord-as-a-chat-extension-of-the-supervisor.md` | `rev-brisk-flare`, approved (revision 1) | The supervisor inbox and supervisor-authored digest. Implemented, opt-in. |
| `2026-10-09-routing-policy-revision-risk-aware-routing-default-assignmen.md` | `rev-azure-flare`, approved (revision 1) | Routing policy shipped in `default-assignment-routing`, which the built-in policy profile carries. |
| `2026-09-24-supervisor-narrative-updates-hourly.md` | `rev-vivid-grove`, approved; frontmatter says implemented | Hourly supervisor reports. Implemented, opt-in. |
| `2026-09-24-discord-mention-routing-to-the-supervisor.md` and its plan | `rev-swift-current`, approved; plan `rev-brisk-impact`, approved (revision 2) | Discord mentions reach the addressed supervisor. Implemented. |
| `2026-09-24-morning-report-playbook-2.md` | `rev-azure-current`, withdrawn | Superseded. The earlier `2026-09-24-morning-report-playbook.md` (no review, status implemented) is what shipped and was enabled on 2026-09-27. |
| `2026-10-06-e5a-b13-factory-policy-train-and-promotion-entry.md`, `…-restored-train-and-supervisor-controls-runbook.md`, `…-train-and-promotion-policy-entry-draft.md` | `rev-eager-beacon`, `rev-stark-horizon`, `rev-amber-harbor`, all withdrawn | No change. Factory policy kept the 2026-09-11 shape. |
| `2026-10-19-retrospective-why-work-stalls-before-reaching-dev-4-weeks-of.md` | `rev-quick-pinnacle`, withdrawn | The evidence behind fast-smooth revision 4, including the patrol's role. |
| `software-factory-policy-simplification-2026-09-11.md` | None; human-authorized 2026-09-11 | The source of [factory-policy.md](../../concepts/factory-policy.md) and the steer-versus-implement split. |

In the repository,
[2026-08-30-projectless-supervisor-design.md](../../superpowers/specs/2026-08-30-projectless-supervisor-design.md)
describes the global supervisor as built, and
[human-ask-cutover-status.md](../design/human-ask-cutover-status.md) tracks the
human-ask cutover.

### What the October changes established

The month's work set the current shape. Later changes should keep it:
- operator decisions as rows, gated centrally;
- policy profiles with review-only playbook import;
- additive capability sync for the supervisor and worker templates;
- recovery holds that escalate on a deadline without lifting;
- the delivery watchdog;
- global-only report authorship with fallbacks;
- the global supervisor as operator executor, with the human keeping review
  decisions.

## Source and tests

| Area | Source | Focused tests |
|---|---|---|
| Supervisor profile and capability sync | [src/profiles/defaults/supervisor/profile.md](../../../src/profiles/defaults/supervisor/profile.md), [src/profiles/capability_sync.py](../../../src/profiles/capability_sync.py), [src/vault.py](../../../src/vault.py) | `aq test tests/test_supervisor_capability_sync.py` |
| Lifecycle, names, wake | [src/messages/session_lens.py](../../../src/messages/session_lens.py), [src/sessions/reconciler.py](../../../src/sessions/reconciler.py), [src/agents/configuration.py](../../../src/agents/configuration.py) | `aq test tests/test_project_supervisors.py tests/test_supervisor_global_lifecycle.py` |
| Turn health, recovery and rotation | [src/sessions/supervisor_health.py](../../../src/sessions/supervisor_health.py), [src/database/queries/message_queries.py](../../../src/database/queries/message_queries.py), [src/messages/session_lens.py](../../../src/messages/session_lens.py) | `aq test tests/test_supervisor_turn_health.py tests/test_supervisor_stall_sweep_script.py` |
| Authority and tokens | [src/commands/supervisor_authority.py](../../../src/commands/supervisor_authority.py), [src/api/scope.py](../../../src/api/scope.py) | `aq test tests/test_supervisor_global_scope.py tests/test_supervisor_global_token_loopback.py` |
| Recovery | [src/database/queries/task_recovery_queries.py](../../../src/database/queries/task_recovery_queries.py) | `aq test tests/test_supervisor_recovery.py tests/test_supervisor_recovery_api.py tests/test_supervisor_failure_triage.py` |
| Stall patrol | [scripts/operator-checks/stall-sweep.py](../../../scripts/operator-checks/stall-sweep.py) | `aq test tests/test_supervisor_stall_sweep_script.py` |
| Delivery watchdog and escalations | [src/escalations/supervisor.py](../../../src/escalations/supervisor.py) | `aq test tests/test_escalation_delivery.py tests/test_escalation_commands.py` |
| Discord inbox | [src/commands/conversation_commands.py](../../../src/commands/conversation_commands.py) | `aq test tests/test_supervisor_inbox_commands.py` |
| Reports and digest | [src/commands/report_commands.py](../../../src/commands/report_commands.py), [src/digest/](../../../src/digest/) | `aq test tests/test_morning_reports.py tests/test_report_requests.py tests/test_report_delivery.py tests/test_digest_supervisor.py tests/test_digest_commands.py tests/test_digest_dispatch.py` |
| Operator decisions | [src/operator_decisions.py](../../../src/operator_decisions.py), [src/commands/decision_commands.py](../../../src/commands/decision_commands.py) | `aq test tests/test_integration_operator_decisions.py` |
| Policy profiles | [src/policy_profiles/](../../../src/policy_profiles/), [src/commands/policy_commands.py](../../../src/commands/policy_commands.py), [src/cli/policy.py](../../../src/cli/policy.py) | `aq test tests/test_policy_profiles.py tests/test_cli_policy.py` |
| Change sets | `aq task batch-propose` / `batch-commit` | `aq test tests/test_cli_task_change_sets.py` |
| Factory policy and playbooks | [docs/concepts/factory-policy.md](../../concepts/factory-policy.md), [src/playbooks/required.py](../../../src/playbooks/required.py) | `aq test tests/test_factory_policy_instructions.py tests/test_required_playbooks.py tests/test_v1_removal.py` |
| Config | [src/config.py](../../../src/config.py) | `aq test tests/test_config_supervisor.py` |

After editing a reviewed playbook bundle such as `supervisor-failure-triage`,
follow the bundle rebuild steps in [AGENTS.md](../../../AGENTS.md). After
editing the shipped supervisor profile, remember that an installed vault copy
changes only through capability sync (grants) or
`aq agent profile-reseed`.

Related reading, in order:
1. [factory-policy.md](../../concepts/factory-policy.md) for the policy;
2. the supervisor profile itself for the duties;
3. [2026-08-30-projectless-supervisor-design.md](../../superpowers/specs/2026-08-30-projectless-supervisor-design.md)
   for the global supervisor;
4. [operator-decisions.md](../design/operator-decisions.md) and
   [policy-profiles.md](../design/policy-profiles.md) for the two durable
   mechanisms;
5. [task-change-sets.md](../../guides/task-change-sets.md),
   [escalations.md](../../guides/escalations.md) and
   [supervisor-hourly-reports.md](../../guides/supervisor-hourly-reports.md)
   for running it.
