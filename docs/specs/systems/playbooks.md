---
tags: [system-spec, playbooks, policy, triggers, runs, agent-cron, object-loop]
---

# System spec: playbooks

**System:** `playbooks`, one of the 18 systems in the Agent Queue system inventory
(vault: `projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, in review on 2026-10-20). The inventory folds two smaller
mechanisms into this system: **agent cron** (durable recurring prompts to a session) and
the **object loop** (bounded evaluation rounds for an external renderer).
**Written:** 2026-10-20, against `main` at `22cffd1b7`, for task `eager-vault-55.8`.
**What this page is:** a map of the system as the code stands, written for an engineer
new to Agent Queue. It describes; it does not decide. The "Planned work" section is a
snapshot of the open tasks and in-flight specs on the date above and goes stale as they
land.

Where this page summarises something another document explains in depth, it links
there instead of repeating it. The everyday reader's page is
[docs/concepts/playbooks.md](../../concepts/playbooks.md); read it first if you have not.
Several of its install counts are out of date (section 6).

> **The whole system is behind a switch that is off by default.** `PlaybooksConfig.enabled`
> defaults to `False` (`src/config.py`, labelled "Paused by default during the framework
> overhaul … Temporary"). With it off the daemon starts no playbook runtime, timer service,
> required-playbook reconciler or resume handler (`src/orchestrator/core.py` ~2156), the
> V2 authoring commands refuse, and the playbook doctor checks are skipped. Everything
> below describes a daemon with `playbooks.enabled: true`, which is how this operator's
> `~/.agent-queue/config.yaml` runs it. Agent cron and the object loop's commands do not
> depend on the switch, but the object loop's policy does.

---

## 1. Purpose: the problem it solves

Agent Queue's rule is **mechanism in code, policy in playbooks**. The daemon's code is
deterministic: it stores tasks, schedules claims, runs sessions, merges branches and
never calls an LLM to decide what happens next. But a running factory needs many
*decisions* that an operator wants to read, change and review without editing Python:

- which agent profile should take this new task;
- what to do when a provider runs out of quota;
- when an approved spec should become a task graph, and who approves the result;
- what to do when a task fails, gets stuck, or the main branch's CI goes red;
- when to write the morning report.

A playbook is that policy, written down. It is a Markdown file in the vault that says
"when *this event* happens, run *these steps*", where each step calls an ordinary daemon
command, asks a model for a typed answer, starts an agent task, branches on data, or
waits. The playbooks system turns that file into an immutable, hash-addressed artifact,
lets an operator review and activate it deliberately, and runs it durably: a run
survives daemon restarts, waits for humans or events for as long as it needs, and never
does anything its reviewed artifact did not declare.

Three things make this different from "a script the daemon runs":

- **Reviewed bytes.** What runs is a compiled artifact pinned by SHA-256, not the
  Markdown. Activation names the hash; changing the source does nothing until someone
  activates a new one.
- **Bounded authority.** An artifact can only call the commands it declares
  (`granted_aq_commands`), each through the same `CommandHandler` the CLI uses, and each
  LLM or agent step runs under a profile no wider than its caller.
- **Typed control flow.** Branches follow declared outcomes. A model's prose never picks
  the next step: an LLM step returns schema-validated JSON and a typed field selects the
  edge.

## 2. Where the code lives

| Concern | Code |
|---|---|
| Artifact schema, canonical bytes, hashing | `src/playbooks/definition.py` (`PlaybookDefinition`; generated `src/playbook_v2_schema.json` via `scripts/generate-playbook-schema.py`) |
| Source frontmatter and identifier inventory | `src/playbooks/authoring.py` |
| Compile (no LLM) and validation | `src/playbooks/proposal.py`, `validation.py`, `pipeline_lowering.py` (deterministic lowering of `kind: pipeline` sources) |
| Artifact storage | `src/playbooks/artifact_store.py`, `artifact_ref.py`, `artifact_tombstone.py`, `artifact_diff.py`, `semantic_diff.py` |
| Activation and health | `src/playbooks/activation.py`, `routing.py` (routing activation snapshot) |
| Engine: dispatch, step walk, resume, cancel | `src/playbooks/engine.py` (`PlaybookEngine`, `WaitScheduler`, `ChildTaskReconciler`) |
| Run lifecycle and limits | `src/playbooks/run_state.py`, `waits.py`, `receipts.py`, `expressions.py`, `retention.py`, `run_overlay.py`, `explanation.py` |
| Step executors | `src/playbooks/executors/` (`command.py`, `llm.py`, `agent_task.py`, `wait.py`, `foreach.py`, `decision.py`, `terminal.py`; table in `__init__.py`; shared types in `base.py`) |
| Daemon wiring | `src/playbooks/runtime.py` (`V2PlaybookRuntime`, the orchestrator's `playbook_manager`), `services.py` (`build_v2_engine`, `DatabaseActivationSource`) |
| Restart recovery and resume | `src/playbooks/recovery.py` (`RestartReconciler`), `resume_handler.py`, `src/workflow_stage_resume_handler.py` |
| Shipped install, required set, receipt | `src/playbooks/required.py` |
| Call-site context | `src/playbooks/invocation.py` (`PlaybookInvocation` ContextVar) |
| Dashboard graph view | `src/playbooks/graph_projection.py`, `src/workflow_pipeline_view.py` |
| Timers | `src/timer_service.py` (`timer.*` and `cron.*` events) |
| Commands | `src/commands/playbook_v2_commands.py`, `playbook_commands.py`; contracts in `src/commands/contracts/` |
| CLI | `src/cli/playbook.py` (only `import` is hand-written; the other 29 verbs are generated) |
| Doctor | `src/doctor/playbook_v2_checks.py` |
| Shipped content | `src/prompts/default_playbooks/`, `integration_playbooks/`, `project_playbooks/`, `reviewed_playbooks/` (authoring copy `tests/fixtures/playbooks/v2/`) |
| Bundle rebuild | `scripts/rebuild-reviewed-playbook-artifacts.py` |
| Agent cron | `src/agent_cron.py`, `src/database/queries/agent_cron_queries.py`, `src/commands/cron_commands.py`, `src/cli/cron.py` |
| Object loop | `src/object_loop/`, `src/commands/object_loop_commands.py`, `src/commands/contracts/object_loop.py`, formulas `src/prompts/formulas/object.md` and `variation.md` |
| Schema | `src/database/tables.py` (`playbook_artifacts`, `playbook_activations`, `playbook_v2_runs`, `playbook_step_receipts`, `playbook_waits`, `playbook_pending_events`, `agent_cron`, `object_loops`) |

`src/playbooks/` is 39 modules and about 19,000 lines; `engine.py` alone is about 3,900.
The [module reference](../../reference/modules/playbooks.md) lists them; the
[playbook commands reference](../../reference/playbook-commands/README.md) is generated,
one page per command a playbook may call.

## 3. How it works today

### 3.1 Key concepts

**Source.** A Markdown file in the vault (`system/playbooks/<id>.md`, or under a project)
with frontmatter `id`, `scope` and `triggers`, and prose that explains the policy to a
human. Backticked identifiers in the prose (command names, event types, profiles) form
an inventory that validation checks against the live registries.

**Scope.** `system` (sees every event), `project:<id>` (sees events for that project)
or `agent_type:<type>`. A project activation does not shadow a system activation of the
same playbook during event dispatch; both run.

**Semantic body.** The machine form of the policy: JSON `rules` and `steps`. A rule has
one trigger (an event type plus literal filters and an optional guard expression) and an
entry step. Steps form a graph whose edges are keyed by outcome. The body is written by
an author or a compiler-agent session; the daemon itself never asks a model to produce
it.

**Artifact.** The validated, canonicalised `PlaybookDefinition`, identified by
`artifact_sha256` over its canonical bytes, with a `source_digest`, a `contract_fingerprint`
over the commands it uses, and `compiled_against` snapshots of the contracts and
profiles it was checked against. At most 50 rules and 500 steps. Its
`granted_aq_commands` are derived from the steps, not authored separately.

**Reviewed bundle.** The unit that ships and installs: `source.md`, `artifact.json`,
`artifact.sha256`, `manifest.md` (hashes, fingerprint, granted commands, referenced
profiles), `diagnostics.json`, and optionally `traces.json` (recorded replays).

**Activation.** One row per (playbook, scope, scope identifier) naming the active
artifact hash, an enabled bit and a health. Only an activation that is enabled, `ready`
and points at an artifact runs.

**Run.** One execution of one rule of one artifact, pinned to that artifact's hash for
its whole life. A run has a mode (`live`, `dry_run`, `shadow`); the mode chooses which
executors run, never a different graph.

**Step kinds** (seven):

| Kind | What it does |
|---|---|
| `command` | Calls one contracted daemon command; its business outcome selects the edge. |
| `llm` | Asks a model under a named profile, with a budget; schema-validated output, a typed field selects the edge. |
| `agent_task` | Creates a child task for an agent; may wait for it to finish. |
| `decision` | Pure branch on data: first true case, else the default. |
| `wait` | Suspends the run until an event, a human decision, a child task or a timer. |
| `foreach` | Iterates a pinned collection (no nesting today) with a halt / collect / continue failure policy. |
| `terminal` | Ends the run with an outcome. |

The [concepts page](../../concepts/playbooks.md) explains the vocabulary with the
routing playbook as a worked example.

### 3.2 Data model

| Table | Holds |
|---|---|
| `playbook_artifacts` | Each compiled artifact by hash (the bytes also live in a content-addressed file store under `compiled_root`). |
| `playbook_activations` | The active hash, `enabled`, `health`, `reasons` per (playbook, scope, identifier); the artifact foreign key is `RESTRICT`, so an active artifact cannot be deleted. |
| `playbook_v2_runs` | One row per run: artifact hash, mode, lifecycle state, snapshot (JSON, versioned), dispatch id. The unique index `uq_playbook_v2_runs_dispatch_rule` (playbook, dispatch, rule) is the event dedupe. |
| `playbook_step_receipts` | One receipt per step boundary (unique per boundary), redacted default-deny. |
| `playbook_waits` | Open waits with their match spec and deadline. |
| `playbook_pending_events` | An inbox of events that arrived for a wait, or for an artifact that was unavailable when the event came in. |
| `agent_cron` | Agent cron schedules (section 3.3.7). |
| `object_loops` | Object-loop state (section 3.3.8). |

Run lifecycle states are `running`, `paused`, `cancelling`, `completed`, `failed`,
`blocked`, `timed_out` and `cancelled`. `src/playbooks/run_state.py` holds the legal
transitions: terminal states have no exits, and a paused run may be cancelled directly.
A step's result is capped at 256 KiB and a snapshot at 4 MiB (both configurable).

Activation health is one of `ready`, `question_required`, `invalid`, `disabled`,
`stale_contract` or `unavailable`, evaluated in a fixed order where the first match wins
(`activation.py`, `evaluate_health`).

Outside the database: the vault holds sources and installed bundles
(`<vault>/system/playbooks/`, `<vault>/reviewed-playbooks/<id>/`); the data directory
holds the first-install receipt `playbook-installation.json` and the timer state
`timer_state.json`.

### 3.3 Main flows

#### 3.3.1 Author, validate, import, activate

1. **Write the source** in the vault and a semantic body beside it.
2. **Propose and validate.** `playbook_v2_propose` (with `semantic_body_path`) strips any
   authority fields the caller supplied, recomputes them, and runs validation against
   the live command contracts, profiles and event registry. Diagnostics are `error`,
   `question` or warnings; an error or question blocks activation. A `kind: pipeline`
   source can instead be lowered deterministically (`update-source`); any other source
   needs a proposal.
3. **Import a bundle.** `aq playbook import --path reviewed-playbooks/<id>` is
   operator-only and refuses a bundle outside the vault root. It checks the artifact is
   canonical and matches its hash, validates the manifest and revalidates. It never
   activates.
4. **Activate.** `aq playbook activate --playbook-id <id> --artifact-sha256 …` (or
   `import --activate`). The command checks the caller may write that scope, computes the
   diff's activation blockers, re-runs validation, and writes `health=ready`.
5. **Review.** A playbook can be submitted as a document review that pins the artifact
   hash ([reviews guide](../../guides/reviews.md#playbook-reviews)). Approval and
   activation are separate acts.

The concepts page's [authoring section](../../concepts/playbooks.md) has the command
sequence; the cutover history is in the
[fresh-cutover design](../../superpowers/specs/2026-09-04-playbook-v2-fresh-cutover-design.md).

#### 3.3.2 An event becomes a run

1. `V2PlaybookRuntime.subscribe_to_events` subscribes to every bus event (`"*"`). Each
   event becomes an asyncio task that calls `engine.dispatch_event` under the service
   principal `playbook-dispatch`.
2. `DatabaseActivationSource.ready_activations` lists every activation and loads each
   ready artifact. This happens on every event; there is no prefilter by trigger.
3. Scope filtering: system activations see all events; project activations need a
   matching `project_id`, except that `timer.*` and `cron.*` events carry no project and
   reach every project activation.
4. Rule matching: exact `event_type`, then literal filters, then the guard expression (a
   guard that fails to resolve counts as false).
5. Each matching rule starts a run. The dispatch id is a hash of the event's
   `event_id`, so the unique index rejects a second run for the same delivery and the
   engine reports `deduplicated`. The event bus stamps a fresh random `event_id` on each
   emit, so this protects against re-delivery of one event, not against the same fact
   being emitted twice.
6. Sibling rules run concurrently with exceptions isolated. There is no cooldown and no
   per-playbook concurrency cap; each run has one driver at a time.

Other ways to start a run: `aq playbook run` (event id `manual:<sha>`),
`aq playbook dry-run` (preview executors, no side effects) and shadow mode (no invoke
path at all).

**Timers.** `src/timer_service.py` reads the trigger set from the runtime and emits
`timer.<N>m|h` for elapsed intervals and `cron.HH:MM` once per local day. Fire times are
persisted to `timer_state.json`, so intervals span restarts and nothing fires on boot;
missed cron days are not back-filled. The service ticks from the orchestrator cycle's
housekeeping.

**Held events.** If the required router `default-assignment-routing` is inactive,
`task.route_needed` events are retained (default 7 days) and replayed once routing is
ready, rather than lost.

#### 3.3.3 Walking steps

`PlaybookEngine._advance_one_step` (`engine.py`) runs one step through fixed stages:
read cancellation; check the deadline (`timed_out`); resume an existing wait instead of
re-executing; resolve inputs (`input_resolution_failed`); authorize; run the executor;
consume the result; and commit. The last three are atomic: the new snapshot, the receipt
and any wait registration commit in one transaction guarded by a snapshot-version
compare-and-swap, so two drivers cannot both advance a run.

**Command steps** are the core path. `LiveCommandExecutor` finds the command's contract
(`src/commands/contracts/registry.py`), builds arguments against its model, and calls
the contract adapter under the contract's timeout. The adapter (`builtin.py`) sets the
principal context and calls `CommandHandler.execute`, so a playbook's state change goes
through the same validation, events and side effects as the CLI. `_consume` is the only
reader of the result: it checks the type, maps the result dict to a declared business
outcome, round-trips the result model and confirms the step has an edge for the outcome.
The `PlaybookInvocation` ContextVar carries the run, step and granted commands into the
handler; routing uses it to accept `task_route_apply` only from the project's bound
router.

**Authorization.** Before execution the engine calls `authorize_command` for the step's
command. A refusal becomes the outcome `unauthorized`, which an edge may route. Runs
dispatched from events use a SERVICE principal, which the resolver treats as trusted; the
effective limit is therefore the artifact's `granted_aq_commands`, which activation
reviewed. `security.capability_enforcement` defaults to `audit`, which records rather than
blocks most violations.

**LLM steps** run under a PLAYBOOK principal narrowed to the step's profile; a profile
wider than the caller is `unauthorized` and a missing one `unavailable`. Tool-free steps
can use a headless CLI (`claude --print`, `codex exec --sandbox read-only`, gemini);
steps with tools use the in-process `llm.complete` / `run_tools` path, and every tool
call is authorized and goes through the contract. None of this runs in tmux: these are
steps, not agents. Budgets bound calls, tokens and time.

**agent_task steps** create a child task under a principal narrowed to parent ∩ child ∩
the step's narrowing. Naming a profile directly is limited to the control-plane roles
(triage, spec-ingest, reviewer, final-reviewer); otherwise the step gives an intelligence
class hint and leaves the child for the router. With `wait_for_completion` the run
suspends until the child settles.

**wait steps** suspend the run. Registration scans the pending-event inbox in the same
transaction, which closes the race where the awaited event arrives just as the wait
begins. Human waits take their deadline from the step or run timeout; there is no
built-in default.

#### 3.3.4 Resuming and recovering

A suspended or interrupted run resumes only through a closed set of causes:
`EventArrived`, `TimerFired`, `ChildTaskCompleted`, `HumanDecision` (idempotent by
decision id, must match the wait and artifact), `OperatorResolution` and
`InterruptedByRestart`. The drivers:

| Driver | Trigger |
|---|---|
| `RestartReconciler` (`recovery.py`) | Runs older than this process start that are still `running` or `cancelling`. |
| `WaitScheduler` (`engine.py`) | Due waits; sends `TimerFired`. |
| `ChildTaskReconciler` (`engine.py`) | Settled child tasks, read from durable state rather than the bus. |
| `PlaybookResumeHandler` (`resume_handler.py`) | `human.review.completed`. |
| `WorkflowStageResumeHandler` | `workflow.stage.completed`, a bridge for legacy workflow references. |

An attempt that was mid-flight at a restart is replayed only when its command contract
is retry-safe or idempotent; otherwise the run pauses with "may have reached an external
side effect" for an operator. An LLM step running at restart pauses the same way.
Cancellation is durable: `cancelling`, a grace period (30 s by default), then
`cancelled`, cascading to child tasks.

#### 3.3.5 Shipping, installing and repairing policy

The repository ships policy in three places. Only the reviewed bundles are installable.

| Directory | Contents |
|---|---|
| `src/prompts/reviewed_playbooks/` | 15 reviewed bundles, byte-identical to `tests/fixtures/playbooks/v2/`. |
| `src/prompts/default_playbooks/` | 7 system sources (the bundles' `source.md` copies). |
| `src/prompts/integration_playbooks/` | 4 integration sources, all `enabled: false`. |
| `src/prompts/project_playbooks/` | agent-queue (4 sources) and matter-engine-cpp (1). |

No Python under `src/` reads `integration_playbooks/` or `project_playbooks/`; they are
used by the rebuild script, tests and docs.

`src/playbooks/required.py` defines three lists:

| List | Ids | Meaning |
|---|---|---|
| `REQUIRED_SYSTEM_PLAYBOOK_IDS` | default-assignment-routing, provider-usage-probe, provider-failover | Gate `/health` and `/ready`; the only ids doctor repairs. |
| `DEFAULT_SYSTEM_PLAYBOOK_IDS` | supervisor-failure-triage, default-pipeline, stuck-task-watch | Activated once, on first install only. |
| `INITIAL_SYSTEM_PLAYBOOK_IDS` | the six above plus morning-report | Copied into an empty vault (morning-report stays inactive). |

**First install.** `install_initial_playbooks`, called from `ensure_vault_layout`
(`src/vault.py`) and the installer (`src/install/prerequisites.py`), acts only when there
is no receipt and the vault is absent or empty. It writes the receipt first, in
exclusive-create mode, with `activation_pending: true`, then copies the seven bundles
and sources. A non-empty vault gets a receipt with `activation_pending: false` and
nothing else. At daemon start, `RequiredPlaybookReconciler.reconcile()` consumes the
receipt (flipping it before writing anything, so a crash cannot activate twice), imports
the bundles and activates the required and default ids.

**Every later restart** only observes. It never installs, refreshes, retires or
reactivates. Repair is explicit: `aq doctor --check playbooks.required --fix` restores
and re-enables the three required ids, and `aq doctor --check playbooks.reviewed_bundles
--fix` refreshes drifted copies of bundles that are already installed (the old copy moves
to `<id>.bak-<epoch>`). Everything else is a manual `aq playbook import … --activate`.
The [isolated acceptance kit](../../guides/e2e-swarm.md) exercises these boundaries.

**Projects.** Project create offers a policy: `default`, `exported` or `none`
(`src/policy_profiles/builtin.py`; non-interactive runs get `none`). The default policy
carries default-pipeline and supervisor-failure-triage at project scope and the routing
binding. Applying it writes `policy/pending-playbooks/<id>/`, submits each playbook for
review and never activates ([policy profiles design](../design/policy-profiles.md)).

**Rebuilding bundles.** `scripts/rebuild-reviewed-playbook-artifacts.py` rebuilds
fixtures from hand-written deterministic body builders (no LLM), rewrites the manifest
digests and checks with `--check`. Nothing in CI or the daemon runs it; its output is
copied by hand to `src/prompts/reviewed_playbooks/` (AGENTS.md, "Reviewed playbook
bundles").

The shipped catalogue, as of this snapshot:

| Playbook | Scope | Triggers | Does | Installed |
|---|---|---|---|---|
| default-assignment-routing | system | `task.route_needed` | `task_route_plan`, `task_route_apply` | required |
| provider-usage-probe | system | `timer.10m` | `provider_usage_probe` | required |
| provider-failover | system | `provider.state_changed`, `timer.5m` | `provider_reroute`, `provider_availability_notify` | required |
| supervisor-failure-triage | system | `task.failed` | `task_failure_triage_notify` (one incident per failure generation) | default |
| default-pipeline | system | `spec.approved`, `proposal.ready`, `gate.resolved` | spec ingestion (3.3.6) | default |
| stuck-task-watch | system | `timer.10m` | `get_stuck_tasks`, `message_send` to the global supervisor | default |
| morning-report | system | `report.morning_due` | `morning_report_tick` | installed, inactive |
| ci-main-sentinel | project:agent-queue | `timer.15m` | CI baseline watch and repair adoption | manual |
| github-issue-triage | project:agent-queue | `cron.02:00`, `review.decided` | nightly issue investigation, fix or close ([guide](../../guides/github-issue-triage.md)) | manual |
| object-loop | project:matter-engine-cpp | `formula.cooked`, task and review events, `timer.5m` | object loop (3.3.8) | manual |
| promotion-continuous, promotion-request | system | `promotion.*` | promotion policy | manual |
| supervisor-digest, supervisor-hourly-report | system | `timer.1m`, `digest.window_ready` | digest and hourly report | manual (bundle only) |
| blocked-task-escalation | system | `task.failed` (`status=BLOCKED`) | retired; kept for audit | never |

#### 3.3.6 Spec ingestion: the default pipeline

The `default-pipeline` playbook is the bridge between an approved spec and a task graph.

1. `spec_approve` (`src/commands/spec_commands.py`) and the ask paths call
   `src/asks/ingestion.py dispatch()`, which records a durable dispatch operation and
   emits `spec.approved`.
2. Rule `spec-ingest-on-approve` calls `ensure_spec_ingest`, deduplicated by
   `spec-ingest:decision:<id>`, which creates one `spec-ingest` role task.
3. Rule `proposal-ready-gate`: on `proposal.ready`, `gate_create` makes a human gate
   awaiting the proposal.
4. Rule `commit-on-gate-resolve`: on an approved human `gate.resolved`,
   `task_batch_commit` commits the proposal.
5. The playbook does not retry. Every orchestrator cycle runs `spec_ingest_reconcile`,
   which re-drives any dispatch still pending.

Held ingestion (the task-graph spec, section 3.3.4) is changing steps 3–4; see 7.1.

#### 3.3.7 Agent cron

Agent cron gives one session a durable recurring prompt. It is not a playbook trigger
and shares no code with `TimerService`; the inventory groups it here because it is the
other way the factory acts on a schedule. The
[agent cron guide](../../guides/agent-cron.md) and
[design](../design/agent-cron.md) match the code.

- A schedule is `every` (60 s to 7 days, epoch-aligned with an offset) or a restricted
  `MINUTE HOUR * * *` cron in a named time zone.
- The owner is the calling session and its instance token. A named supervisor's schedule
  is projectless; any other session's is bound to its held task and claim epoch and
  expires when that stops being current. Sleeping a session cancels its schedules.
- Limits: 10 active schedules per session, 5 delivery attempts, a pending prompt expires
  after an hour. Registration is idempotent by key.
- Each tick (inside the cycle's session reconcile) sends a `schedule_prompt` message
  `cron:<id>:<tick>` from `system:agent-cron`. If a prompt is still pending, the tick
  coalesces instead of queueing a second one. Retries back off exponentially from 30 s.
  Busy, sleeping and absent sessions are skipped, never cold-started.
- Commands: `cron_register`, `cron_get` (with `--consume`), `cron_list`, `cron_cancel`
  (`aq cron …`). The supervisor's patrol uses the key `supervisor-patrol-v1`.

#### 3.3.8 The object loop

The object loop coordinates evaluation rounds for an external renderer (Matter): each
round is a wave of one to three candidate variants, then a scorer task, then optional
checkpoints, and a finalizer task closes the loop. State is one `object_loops` row per
object. The [object loop guide](../../guides/object-loop.md) and
[design](../design/object-script-loop.md) cover the details.

- **Formulas.** `object` and `variation` (`src/prompts/formulas/`) create the loop and
  each variation; their guards run inside the graph transaction under an advisory lock
  per object (`src/object_loop/formulas.py`).
- **Commands.** `object_loop_start`, `object_loop_reconcile`, `object_score_record`,
  `object_checkpoint_read`, `object_loop_inputs`, plus `artifact_verify` for workers.
- **Guards.** Budgets in usd, calls, bakes and active seconds (unknown spend is charged
  at the full reservation); caps on rounds (8), repair rounds (2) and plateau rounds (3);
  a winner is promoted only when `winner + noise_band < incumbent`.
- **Policy.** The reviewed `object-loop` bundle, scoped to `project:matter-engine-cpp`,
  ships disabled; its five granted commands are the only way it moves the loop. Review
  decisions, provider routing, publishing and activation stay outside it.

## 4. Boundaries and interactions

The inventory's other 17 systems, and where the line falls with each. "Owns" means the
other system decides; playbooks only call its commands or consume its events.

| System | Interaction |
|---|---|
| **daemon-core** | Owns the event bus, the orchestrator cycle, the schema and migrations. Playbooks subscribe to the bus, contribute cycle steps (timer tick, restart reconciler, wait scheduler, child reconciler, `spec_ingest_reconcile`, agent cron tick) and are started or skipped by `playbooks.enabled` in `Orchestrator.initialize`. |
| **task-graph** | Owns tasks, edges, gates, formulas and batches. Playbooks create tasks, gates and batch commits through its commands; `agent_task` steps make child tasks; object-loop formulas are graph templates with guards. |
| **scheduling-and-claims** | Owns the frontier and claims. Playbooks never claim; a task a playbook creates is scheduled like any other. |
| **routing-and-profiles** | Owns profiles, intelligence classes and the route. The required `default-assignment-routing` playbook *is* the router's policy (`task_route_plan`/`task_route_apply`); `src/routing/` is the mechanism. LLM and agent steps run under profiles, which validation fingerprints. |
| **sessions-and-harnesses** | Owns agent sessions. Playbook LLM steps are not sessions (in-process or headless CLI); agent cron delivers prompts to sessions. |
| **projects-workspaces-git** | Owns projects. Project creation offers the default policy and writes pending playbooks; project scope is how a playbook is limited to one project. |
| **integration-and-delivery** | Owns trains, promotion and delivery. Promotion policy and (planned) train policy are playbooks over its primitives; `INTEGRATION_LIFECYCLE_PLAYBOOK_IDS` re-points those activations to the route the integration layer chooses. `ci-main-sentinel` watches the main CI baseline. |
| **messaging-and-human-asks** | Playbooks send messages (`message_send`), open human gates and waits; spec approval through an ask emits `spec.approved`. Agent cron prompts are messages. |
| **supervisors-and-policy** | Supervisors receive what playbooks notice (failure triage, stuck tasks, object-loop refusals) and activate optional policy such as github-issue-triage. The supervisor patrol is an agent cron schedule. |
| **reviews-and-knowledge** | A playbook can be reviewed with its artifact hash pinned; `review.decided` and `human.review.completed` resume or trigger runs; spec approval starts ingestion. |
| **providers** | `provider-usage-probe` and `provider-failover` are the required policy over provider availability ([provider failover spec](../provider-failover.md)). |
| **configuration-and-vault** | Supplies `playbooks.*`, `security.capability_enforcement` and paths; the vault holds sources and installed bundles, written only if absent. |
| **api-and-clients** | Exposes playbook commands, the graph view and run inspection; the playbook commands reference is generated. |
| **dashboard** | Renders the playbook graph and run overlays from `graph_projection.py` and the pipeline view; it never runs policy. |
| **test-service-and-jobs** | No direct link today; the building-blocks refactor (7.1) adds durable script jobs. |
| **operations-and-recovery** | Doctor checks (`playbooks.required`, `reviewed_bundles`, `activation_stale`, `orphaned_runs`, …) and operator resolution of paused runs; `stuck-task-watch` and `supervisor-failure-triage` feed recovery. |
| **plugins-mcp-and-tools** | MCP tools are another front door to the same playbook commands; LLM steps call tools only through command contracts. |

## 5. Design principles and invariants

1. **Mechanism in code, policy in playbooks.** Scheduling never calls an LLM; judgment
   is a playbook calling commands. Compiling a playbook is pure; the semantic body comes
   from an author.
2. **What runs is a reviewed hash.** A run is pinned to an artifact's SHA-256 for its
   whole life; activation names the hash; a source edit changes nothing until a new hash
   is activated.
3. **Every state change goes through `CommandHandler`.** A command step calls the same
   command as the CLI, through a typed contract with a declared outcome set and timeout.
4. **Authority is declared and only narrows.** An artifact can call only its
   `granted_aq_commands`; LLM and child-task steps run under a principal no wider than
   their caller; only the bound router may apply a route.
5. **Outcomes, not prose, choose edges.** Decisions are pure expressions; LLM output is
   schema-validated and a typed field selects the edge; result classification is for
   receipts only.
6. **One boundary, one transaction.** Snapshot, receipt and wait registration commit
   together under a version compare-and-swap; a wait registers and scans its inbox in
   that same transaction.
7. **Resume only for a named cause.** The resume causes are a closed set; a possibly
   side-effecting interrupted attempt pauses for an operator instead of replaying.
8. **Restart never installs or activates.** First install is a one-shot receipt; later
   repair is an explicit doctor `--fix` or operator import.
9. **V2 only.** The V1 compiler, runner, manager and tables are gone;
   `tests/test_v1_removal.py` is the ratchet.

## 6. Known gaps

**Defaults and switches**

- **The system is off by default** (see the note at the top). `src/playbooks/routing.py`
  treats a *missing* `playbooks` config as enabled, which disagrees with the default.
- **Capability enforcement is audit by default.** `security.capability_enforcement`
  defaults to `audit` (`src/config.py`), and event-dispatched runs use a trusted SERVICE
  principal, so the real limit is the artifact's grants and the activation that
  reviewed them.

**Behaviour that contradicts the docs**

- **`update-source` sets an activation `ready` directly** (`playbook_commands.py`
  ~670–679), bypassing health evaluation and the activate blockers; the concepts page
  says it is not a shortcut around deliberate activation.
- **No cooldown or concurrency cap.** `engine.py` (~520) says `PlaybookManager` applies
  role shadowing, cooldown and a concurrency cap; that class no longer exists and V2
  dispatch applies none of them.
- **Timer docstring.** `src/timer_service.py` says interval timers fire on startup and
  are not persisted; the code persists to `timer_state.json` and does not fire on boot.

**Events and scale**

- **Run events cannot chain.** The engine emits `playbook.v2.run.started`,
  `playbook.v2.step.completed` and `playbook.v2.run.finished`, none of which is in
  `src/event_schemas.py`; the registered `playbook.run.completed` / `failed` are never
  emitted. One playbook cannot trigger on another's completion.
- **Every event scans every activation** and loads each ready artifact, with no trigger
  prefilter.
- **Double runs across scopes.** A playbook active at system and project scope runs
  twice for one event; dispatch does not shadow.
- **Dedupe is per delivery.** The bus mints a random `event_id` per emit, so emitting
  the same fact twice starts two runs.

**Policy in code**

- `engine.py` (~586–593) suppresses three default-pipeline review rules by name, and
  `services.py` (~272–275) special-cases `pr-merge-sweep`. Both are policy living in the
  mechanism.

**Shipped content**

- **Bundles without sources:** supervisor-digest, supervisor-hourly-report and
  blocked-task-escalation (sources deleted in `1c0400b35`; not in the rebuild script's
  list). `src/main.py` gates hourly-report readiness on a playbook that is never
  installed by default.
- **Sources without bundles:** root-train and parent-integration (both copies).
  `INTEGRATION_LIFECYCLE_PLAYBOOK_IDS` still names the retired `hierarchical-delivery`
  and `root-integration-train`, kept to fence surviving activations.
- **Upgrades leave stale vault copies.** Nothing refreshes a drifted bundle at startup,
  and the doctor check covers only bundles already installed.
- **The rebuild is manual.** No CI step or daemon runs the rebuild script; its output is
  copied by hand.
- **github-issue-triage** says `enabled: true` in frontmatter but ships inactive, and
  hard-codes the project and repository in `github_issue_commands.py`.
- **object-loop traces are stale.** `traces.json` pins artifact `e9a2e756…` while the
  bundle's `artifact.sha256` is `e1630040…` (re-stamped in `122f3333b` without
  re-recording). The guide names commands that do not exist (`aq playbook v2-import`,
  `aq object_loop inputs`).

**Dead and half-built code**

- `src/orphan_workflow_recovery.py` (~690 lines) is never constructed (only a `hasattr`
  guard in `core.py`) and calls V1 queries that no longer exist.
- `engine.py` (~1036) notes "Projection tasks are not yet created by the V2 engine".
- `src/playbooks/profiles.py` has no importer in `src/`; `resume_handler.py` has unused
  aliases; `pipeline_lowering.py` is only on the `update-source` and shadow-compile
  paths; `ensure_default_agent_type_playbooks` reads a directory that does not exist;
  the V1 `src/playbook_schema.json` survives for one test.
- Stale comments: `PlaybookManager` in `timer_service.py` and `routing.py`;
  `acknowledge_diff` in the `playbook_activate` docstring; `stop_task` called
  "uncontracted" in `executors/agent_task.py`; `task.failed` called the reflection
  playbook's trigger in `orchestrator/execution.py` and `sessions/reconciler.py`.

**Designs that were never built**

- **Sandboxed playbooks** ([design](../design/sandboxed-playbooks.md)): no playbook-level
  `profile_id`; V2 scopes capability per step instead.
- **Reflection** ([spec](../reflection.md)): superseded; no reflection playbook ships,
  and the vault template is V1-shaped.
- **Mandatory triage** ([design](../../superpowers/specs/2026-08-31-mandatory-triage-playbook-design.md)):
  superseded by required routing and `src/routing/`.

**Stale docs**

- [Concepts](../../concepts/playbooks.md) says first install brings "exactly six"
  playbooks and omits stuck-task-watch (it is seven, six activated), and implies only
  ci-main-sentinel ships a project bundle (github-issue-triage and object-loop do too).
- [Module reference](../../reference/modules/playbooks.md) says only routing is required
  (three are).
- `profile.md` lists V1 modules (`compiler`, `runner`, `manager`, …) that are gone.
- [Trust and ops](../design/trust-and-ops.md) says startup refreshes drifted bundles.
- [Default tuning](../../guides/default-tuning.md) and the
  [blocked-task escalation design](../../superpowers/specs/2026-09-06-blocked-task-escalation-design.md)
  call blocked-task-escalation shipped and enabled.
- Retired ids (`hierarchical-delivery`, `root-integration-train`, `coding-reflection`,
  `pr-merge-sweep`, `task-outcome`, `code-quality-gate`) still appear in generated command
  pages, runbooks and the [V1 design](../design/playbooks.md).
- The rebuild script's docstring speaks of "two of the four shipped playbooks" (it lists
  12), and the AGENTS.md step "update the manifest.md digests" is now done by the script.

## 7. Planned work

Snapshot taken 2026-10-20 by a read-only query of the open `agent-queue` tasks and the
vault specs. Vault specs are not in this repository; their review id and state are
quoted as they stood that day.

**The headline:** the reusable-building-blocks refactor is complete in the task graph
but not on the development branch. Its implementation epic `bright-grove-21` (leaves
A1–F2) completed 2026-10-19, and `agile-stone-80` (merge F2 and land) completed
2026-10-20 with CI green at `cbe588a70` (PR #1689), but its integration checkpoint is
still `working` and the branch is not an ancestor of `22cffd1b7`. Most open playbook
work waits for that landing. On the refactor branch only:

- generated command descriptors, so every `CommandHandler` command becomes callable from
  a playbook except `run_command`, `shutdown`, `restart_daemon` and `update_and_restart`;
- encrypted private values, filter/map/len/diff expressions, durable nested `foreach`;
- `doctor_run` and immutable observation snapshots with paging;
- durable playbook-local state with notify-once;
- a `script` step backed by durable jobs (`src/jobs/`);
- migrations `a00000000103`–`108`;
- a rewritten `stuck-task-watch` (pure reviewed Markdown) plus a new
  `stuck-task-watch-reminder` bundle, with every shipped bundle's digests changed;
- guides `playbook-building-blocks`, `playbook-script-runner`, `playbook-state`,
  `observation-snapshots`.

### 7.1 Open tasks that change playbooks

| Task | State | What it changes |
|---|---|---|
| `wise-orbit-24` — refactor F3: acceptance, migration and rollback rehearsal, activation handoff | BLOCKED (on the landing, recorded only in its text) | End-to-end acceptance on this box, a migration and rollback rehearsal, and deliberate install and activation, including private-value key setup so generated invocation works. Real-host isolation acceptance was dropped. |
| `agile-vault-38` — playbook scripts refused by default | fixed on its branch | Scripts failed with `script_storage_or_runtime_unconfigured` unless four `scripts.*` roots were set. The roots now default under `~/.agent-queue/scripts/`, host executables run without a manifest, digest pinning is opt-in through `runtime_manifest`, and the module is `src/jobs/script_runner.py`. Script steps still need `resources.jobs.enabled`, which defaults off. |
| `clear-impact-72` — evaluate rewriting shipped playbooks onto the new building blocks | IN_PROGRESS; `.1`–`.11` READY, each blocked on `agile-stone-80` | One evaluation per bundle: ci-main-sentinel, default-assignment-routing, default-pipeline, github-issue-triage, morning-report, object-loop, promotion-continuous, promotion-request, provider-failover, provider-usage-probe, supervisor-failure-triage, using stuck-task-watch as the reference. A yes writes a vault plan `plans/2026-10-20-playbook-rewrite-<id>.md` and files one task; a no records why. None edits a playbook. The three source-less bundles are not covered. |
| `nimble-lantern-37.2` — held ingestion L5.2 | READY | default-pipeline gains `fidelity-review-on-held` and `release-on-receipt`; the spec-ingest text changes; the `task_batch_commit` grant leaves the spec-ingest profile; the bundle is rebuilt. Ships with L1.2. `nimble-lantern-37.3` (DEFINED) documents it. |
| `stark-willow-95.17` — fast smooth integration S4 | DEFINED | Aligns shipped factory policy, prime, templates, skills and reviewed bundles with the new integration contract; deprecates `cadence_seconds` and `settling_cap_seconds`; adds a ratchet; rebuilds the reviewed policy artifacts. |
| `bold-zenith-11.22` — hibernation cutover | READY | Refuses v1 wait bodies (`wait.upgrade_required`), which touches playbook wait steps; doctor checks and a runbook. |
| `sharp-cascade-53.2`, `calm-orbit-69.3` | PAUSED / DEFINED | Held-ingestion recovery and CLI; indirect. |
| `eager-vault-55` — system map part 2 | IN_PROGRESS | This page and its 17 siblings. |

### 7.2 In-flight specs

| Spec | State on 2026-10-20 | What it changes |
|---|---|---|
| Playbooks can do anything: reusable building blocks (vault, `specs/2026-10-12-playbooks-can-do-anything-reusable-building-blocks.md`) | approved, `rev-steady-ridge` revision 1 | The design behind the refactor above. Acceptance: the stuck-task watch as pure reviewed Markdown with no watch-specific Python. Not permission to activate. |
| Its implementation spec (vault, `specs/2026-10-12-implementation-playbooks-reusable-building-blocks.md`) | approved, `rev-smart-flare` revision 1 | Leaves A1–F3 and the limits: snapshots 30 min, 500 rows / 192 KiB a page, quotas refused rather than evicted; expressions depth 10; nested loops depth 4; state 256 KiB a value, 10k keys. Rollout starts disabled, and the watch never joins the default install. Its sandbox sections (bubblewrap, cgroups) are stale; see the next row. |
| No-sandbox decision (vault, `specs/2026-10-14-playbook-scripts-no-sandbox-decision.md`) | decision notes, no review | Drops namespace, cgroup and network isolation, the launcher and the doctor gate. Keeps script/argv/shell steps with data inputs, timeouts, output caps, JSON binding, and a switch that defaults on. |
| Deliberate playbook installation and project policy (vault, `specs/2026-10-11-deliberate-playbook-installation-and-project-policy.md`) | approved, `rev-fresh-journey` revision 1 | Implemented (`3b6fb9578`, `1c0400b35`): section 3.3.5. |
| Shared rules, subgraphs and view (vault, `plans/2026-10-11-playbook-graph-shared-rules-subgraphs-and-view.md`) | draft, no review, no task | object-loop compiles to five copies of a 21-step sweep (105 steps, 295 edges). Phases: multi-trigger rules; subgraph inline frames (risky); failure edges as a hover glyph; per-container layout. Suggested order 1, 4, 3, 2. Not built anywhere. |
| Fast smooth integration, rev3 playbook policy (vault, `specs/2026-10-13-fast-smooth-integration-rev3-playbook-policy.md`, folded into `specs/2026-10-12-fast-smooth-integration.md`) | decision notes; the spec's review `rev-fair-lantern` is approved at revision 2 while its text is revision 3 | Train policy becomes a per-project playbook over daemon primitives, and the playbook is the on/off switch; required checks become playbook data; "green wins"; every wait has a due time. Carried out by `stark-willow-95`. |
| Spec ingestion held batches (vault, `2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md`) | approved, `rev-agile-beacon` revision 1 | Source of `nimble-lantern-37` and the default-pipeline change above. |
| Agent Queue system inventory (vault, `2026-10-20-agent-queue-system-inventory.md`) | in review, `rev-bright-rapids` revision 1 | The frame for this page. |

Implemented recently and listed here for orientation: mandatory task routing (vault
`2026-09-28-mandatory-task-routing.md`, `rev-vivid-impact`, built in `56f919ffb`); the
risk-aware routing revision and its activation handoff (`2026-10-09`, `rev-azure-flare`
and `rev-wise-impact`; repo
[risk-aware routing](../../superpowers/specs/2026-10-09-risk-aware-routing/README.md));
integration policy tables
([repo spec](../../superpowers/specs/2026-10-02-integration-policy-tables.md),
`0bed9c77d`); the morning report (`2026-09-24`, built in `612060e0e`; its `-2` revision
`rev-azure-current` was withdrawn); the project-defaults playbook (`2026-09-27`,
`rev-crisp-nexus` revision 2 and `rev-eager-grove` revision 1, built as the vault's
`system/playbooks/project-defaults.md`; the companion design's `rev-wise-nexus` was
withdrawn although its frontmatter still says `in_review`).

## 8. Further reading

Current behaviour:

- [Playbooks](../../concepts/playbooks.md) — the concepts page; start here.
- [Agents and routing](../../concepts/agents-and-routing.md) — the router playbook in context.
- [Playbook commands reference](../../reference/playbook-commands/README.md) (generated)
  and [contracts](../../reference/cli/contracts.md).
- [Module reference](../../reference/modules/playbooks.md).
- [Agent cron guide](../../guides/agent-cron.md), [object loop guide](../../guides/object-loop.md),
  [GitHub issue triage](../../guides/github-issue-triage.md),
  [escalations](../../guides/escalations.md), [reviews](../../guides/reviews.md#playbook-reviews).
- [Provider failover](../provider-failover.md), [policy profiles](../design/policy-profiles.md),
  [e2e swarm kit](../../guides/e2e-swarm.md).

Design records (historical; the code wins where they disagree):

- [Playbook V2 semantic graph](../../superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md)
  and [fresh cutover](../../superpowers/specs/2026-09-04-playbook-v2-fresh-cutover-design.md) — the V2 design.
- [Playbooks (V1 design)](../design/playbooks.md) — LLM-chosen transitions; describes the removed V1 engine.
- [Sandboxed playbooks](../design/sandboxed-playbooks.md), [reflection](../reflection.md),
  [mandatory triage](../../superpowers/specs/2026-08-31-mandatory-triage-playbook-design.md),
  [playbook intelligence routing](../../superpowers/specs/2026-08-31-playbook-intelligence-routing-design.md),
  [blocked-task escalation](../../superpowers/specs/2026-09-06-blocked-task-escalation-design.md).
- [Agent cron design](../design/agent-cron.md), [object script loop design](../design/object-script-loop.md),
  [feature pauses](../design/feature-pauses.md), [trust and ops](../design/trust-and-ops.md).
