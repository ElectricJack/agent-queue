---
tags: [system-spec, playbooks, policy, triggers, runs, agent-cron, object-loop]
---

# System spec: playbooks

**System:** `playbooks`, one of the 18 systems in the Agent Queue system inventory
(vault: `projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`,
review `rev-bright-rapids`, in review on 2026-10-20). The inventory folds two smaller
mechanisms into this system: **agent cron** (durable recurring prompts to a session) and
the **object loop** (bounded evaluation rounds for an external renderer).
**Written:** 2026-10-20 for task `eager-vault-55.8`.
**Audited:** 2026-10-22 against `origin/dev` at `52d2efe4c` for `solid-glacier-69`.
**What this page is:** a map of current checked-in behavior for an engineer new to
Agent Queue. It describes shipped resources, not the operator's installed policy or
historical task status. Start with [Playbooks V2](../../concepts/playbooks.md).

> **Configuration:** `playbooks.enabled` defaults to `false` in code; installation
> tuning sets it to `true`. While disabled, the daemon starts no playbook runtime,
> timer service, required-policy reconciler or human-review resume handler, and V2
> authoring commands refuse. Agent cron and object-loop commands are independent of
> this switch; executing object-loop policy still requires it. The following
> describes an enabled runtime.

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
  Markdown. Activation names the hash; normal source edits do not replace it. The
  legacy pipeline `update-source` exception is recorded in section 6.
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
| Step executors | `src/playbooks/executors/` (`command.py`, `llm.py`, `agent_task.py`, `script.py`, `wait.py`, `foreach.py`, `decision.py`, `terminal.py`; table in `__init__.py`; shared types in `base.py`) |
| Daemon wiring | `src/playbooks/runtime.py` (`V2PlaybookRuntime`, the orchestrator's `playbook_manager`), `services.py` (`build_v2_engine`, `DatabaseActivationSource`) |
| Restart recovery and resume | `src/playbooks/recovery.py` (`RestartReconciler`), `resume_handler.py`, `src/workflow_stage_resume_handler.py` |
| Shipped install, required set, receipt | `src/playbooks/required.py` |
| Compatibility and authority | `src/playbooks/features.py`, `authority.py`, `private_values.py` |
| Local state and notification episodes | `src/playbooks/state.py`, `state_notify.py`, `state_preview.py` |
| Script pinning and execution policy | `src/playbooks/script_inputs.py`, `script_policy.py`, `src/jobs/script_runner.py` |
| Integration decision data | `src/playbooks/integration_policy.py`, `train_policy.py` |
| Call-site context | `src/playbooks/invocation.py` (`PlaybookInvocation` ContextVar) |
| Dashboard graph view | `src/playbooks/graph_projection.py`, `src/workflow_pipeline_view.py` |
| Timers | `src/timer_service.py` (`timer.*` and `cron.*` events) |
| Commands | `src/commands/playbook_v2_commands.py`, `playbook_commands.py`; contracts in `src/commands/contracts/` |
| CLI | `src/cli/playbook.py` (`import` is a hand-written wrapper; the other verbs come from the command registry) |
| Doctor | `src/doctor/playbook_v2_checks.py` |
| Shipped content | `src/prompts/default_playbooks/`, `integration_playbooks/`, `project_playbooks/`, `reviewed_playbooks/` (authoring copy `tests/fixtures/playbooks/v2/`) |
| Bundle rebuild | `scripts/rebuild-reviewed-playbook-artifacts.py` |
| Agent cron | `src/agent_cron.py`, `src/database/queries/agent_cron_queries.py`, `src/commands/cron_commands.py`, `src/cli/cron.py` |
| Object loop | `src/object_loop/`, `src/commands/object_loop_commands.py`, `src/commands/contracts/object_loop.py`, formulas `src/prompts/formulas/object.md` and `variation.md` |
| Schema | `src/database/tables.py` (`playbook_artifacts`, `playbook_activations`, `playbook_v2_runs`, `playbook_step_receipts`, `playbook_waits`, `playbook_pending_events`, `agent_cron`, `object_loops`) |

The [module reference](../../reference/modules/playbooks.md) maps implementation
files. The [playbook commands reference](../../reference/playbook-commands/README.md)
is generated from the same command contracts used by validation and execution.

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

**Step kinds** (eight):

| Kind | What it does |
|---|---|
| `command` | Calls one contracted daemon command; its business outcome selects the edge. |
| `llm` | Asks a model under a named profile, with a budget; schema-validated output, a typed field selects the edge. |
| `agent_task` | Creates a child task for an agent; may wait for it to finish. |
| `decision` | Pure branch on data: first true case, else the default. |
| `wait` | Suspends the run until an event, a human decision, a child task or a timer. |
| `foreach` | Iterates a pinned collection with a halt / collect / continue failure policy; durable nesting is limited to four levels. |
| `script` | Runs a source-declared, server-pinned program as a durable managed job with bounded, protected output. |
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
| `playbook_private_values` | Encrypted command bindings, state and protected execution data; public projections carry handles. |
| `observation_snapshots`, `observation_snapshot_pages` | Immutable diagnostic observations with finite page tokens and a 30-minute TTL. |
| `playbook_state_namespaces`, `playbook_state_entries`, `playbook_state_operations` | Versioned playbook-local state, deletions and notification episodes; see [state](../../guides/playbook-state.md). |
| `jobs` | Durable script admission, execution and settlement, including protected output. |
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
   canonical and matches its hash, validates the manifest and revalidates. Import
   alone does not activate; the wrapper accepts an explicit `--activate`.
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

**Authorization.** Command steps pass through contract authorization. For artifacts
with `trusted_execution`, `authority.py` records the initiating caller and binds
effects to that caller intersected with artifact grants and scope. Recovery by an
operator does not substitute operator authority for the recorded caller. Trusted
local/service starts are bounded by the artifact itself; session starts can only
narrow their current and recorded grants. `security.capability_enforcement` defaults
to `audit`; command-specific ownership, scope and human-decision checks still apply.
Feature support and catalogue discovery do not grant execution authority.

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

**Script steps** pin source inputs and optional runtime digests on proposal, then
submit durable jobs. They run ordinary host processes: no OS sandbox or network
isolation. Script admission defaults on, but managed jobs default off and protected
storage keys are required. Queue/wall/output limits, cancellation and ambiguous
launch recovery are described in the [script guide](../../guides/playbook-script-runner.md).
Dry-run and shadow launch nothing.

**Reusable observations and state** let policy inspect finite immutable snapshots,
combine collection expressions and write revision-checked state. Notification episodes
survive restarts and recompilation, avoiding repeated alerts for unchanged findings.
The optional stuck-task watch is built from these primitives, without watch-specific
Python. See [building blocks](../../guides/playbook-building-blocks.md),
[observations](../../guides/observation-snapshots.md) and [state](../../guides/playbook-state.md).

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
| `src/prompts/reviewed_playbooks/` | 17 reviewed bundles, mirrored under `tests/fixtures/playbooks/v2/`. |
| `src/prompts/default_playbooks/` | System policy sources; their reviewed bundle copies are the installable bytes. |
| `src/prompts/integration_playbooks/` | Integration policy sources, including the optional fast-smooth train policy. |
| `src/prompts/project_playbooks/` | Project policies for agent-queue and matter-engine-cpp. |

No Python under `src/` reads `integration_playbooks/` or `project_playbooks/`; they are
used by the rebuild script, tests and docs.

`src/playbooks/required.py` defines three lists:

| List | Ids | Meaning |
|---|---|---|
| `REQUIRED_SYSTEM_PLAYBOOK_IDS` | default-assignment-routing, provider-usage-probe, provider-failover | Gate `/health` and `/ready`; required-policy repair can restore and re-enable them. |
| `DEFAULT_SYSTEM_PLAYBOOK_IDS` | supervisor-failure-triage, default-pipeline | Activated once, on first install only. |
| `INITIAL_SYSTEM_PLAYBOOK_IDS` | the five activated ids above plus morning-report | Copied into an empty vault (morning-report stays inactive). |

**First install.** `install_initial_playbooks`, called from `ensure_vault_layout`
(`src/vault.py`) and the installer (`src/install/prerequisites.py`), acts only when there
is no receipt and the vault is absent or empty. It writes the receipt first, in
exclusive-create mode, with `activation_pending: true`, then copies the six bundles
and sources. A non-empty vault gets a receipt with `activation_pending: false` and
nothing else. At daemon start, `RequiredPlaybookReconciler.reconcile()` consumes the
receipt (flipping it before writing anything, so a crash cannot activate twice), imports
the bundles and activates the required and default ids.

**Every later restart** only observes. It never installs, refreshes, retires or
reactivates. Repair is explicit: `aq doctor --check playbooks.required --fix` restores
and re-enables the three required ids, and `aq doctor --check playbooks.reviewed_bundles
--fix` refreshes drifted copies of bundles that are already installed (the old copy moves
to `<id>.bak-<epoch>`) and imports their reviewed artifacts. This explicit upgrade repair
also repairs enabled system defaults, even if a previous attempt already refreshed the
bundle files. A healthy default advances when its active hash matches the superseded
installed bundle; a different compatible operator artifact remains active. Missing or
disabled optional activations and all project activations remain unchanged. Import
failures fail the repair. Everything else is a manual `aq playbook import … --activate`.
The [isolated acceptance kit](../../guides/e2e-swarm.md) exercises these boundaries.

**Projects.** Project create offers a policy: `default`, `exported` or `none`
(`src/policy_profiles/builtin.py`; non-interactive runs get `none`). The default policy
carries default-pipeline and supervisor-failure-triage at project scope and the routing
binding. Applying it writes `policy/pending-playbooks/<id>/`, submits each playbook for
review and never activates ([policy profiles design](../design/policy-profiles.md)).

**Rebuilding bundles.** `python scripts/rebuild-reviewed-playbook-artifacts.py <id>`
uses deterministic semantic-body builders, validates against current contracts and
updates the authoring fixture, its manifest digests, recorded inputs under
`tests/fixtures/generated_sources/playbooks/<id>/` and the shipped copy.
`scripts/generate-recorded-artifacts.py` projects those approved recorded inputs
unchanged. Review the generated diff; `--check` detects drift. Neither operation
installs into a live vault or changes an activation.

The shipped catalogue, as of this snapshot:

| Playbook | Scope | Triggers | Does | Installed |
|---|---|---|---|---|
| default-assignment-routing | system | `task.route_needed` | `task_route_plan`, `task_route_apply` | required |
| provider-usage-probe | system | `timer.10m` | `provider_usage_probe` | required |
| provider-failover | system | `provider.state_changed`, `timer.5m` | `provider_reroute`, `provider_availability_notify` | required |
| supervisor-failure-triage | system | `task.failed` | `task_failure_triage_notify` (one incident per failure generation) | default |
| default-pipeline | system | `spec.approved`, `proposal.ready`, `gate.resolved` | spec ingestion (3.3.6) | default |
| stuck-task-watch, stuck-task-watch-reminder | system | `timer.15m` | Immutable observations, collection expressions, revision-checked state and notify-once (reminder variant repeats after 24 hours) | manual |
| morning-report | system | `report.morning_due` | `morning_report_tick` | installed, inactive |
| ci-main-sentinel | project:agent-queue | `timer.15m` | CI baseline watch and repair adoption | manual |
| github-issue-triage | project:agent-queue | `cron.02:00`, `review.decided` | nightly issue investigation, fix or close ([guide](../../guides/github-issue-triage.md)) | manual |
| object-loop | project:matter-engine-cpp | `formula.cooked`, task and review events, `timer.5m` | object loop (3.3.8) | manual |
| fast-smooth-train | system (resolved per integration route) | `integration.readiness_changed` | Reviewed train action/check data over daemon merge, repair and publication primitives | manual |
| promotion-continuous, promotion-request | system | `promotion.*` | promotion policy | manual |
| supervisor-digest, supervisor-hourly-report | system | `timer.1m`, `digest.window_ready` | digest and hourly report | manual (bundle only) |
| blocked-task-escalation | system | `task.failed` (`status=BLOCKED`) | retired; kept for audit | never |

#### 3.3.6 Spec ingestion: the default pipeline

The `default-pipeline` playbook is the bridge between an approved spec and a task graph.

1. `spec_approve` (`src/commands/spec_commands.py`) and the ask paths call
   `src/asks/ingestion.py dispatch()`, which records a durable dispatch operation and
   emits `spec.approved`.
2. Rule `spec-ingest-on-approve` calls `ensure_spec_ingest`, deduplicated by
   `spec-ingest:decision:<id>`, which creates one `spec-ingest` role task and pins
   the approved decision and packet receipt.
3. Approved-spec ingestion commits its server-stamped batch directly after
   validation. Ordinary proposals emit `proposal.ready`; rule `proposal-ready-gate`
   then creates a human gate and freezes that proposal revision.
4. Rule `commit-on-gate-resolve`: on an approved human `gate.resolved`,
   `task_batch_commit` commits the proposal.
5. The playbook does not retry. Every orchestrator cycle runs `spec_ingest_reconcile`,
   which re-drives any dispatch still pending.

The checked-in source has these three rules. Historical held-ingestion proposals
do not describe additional rules in the shipped bundle.

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

- **Formulas.** `object` and `variation` (`src/prompts/formulas/`) create the loop epic and
  final-suite task; the reconciler creates bounded candidate/scorer waves. Formula
  guards run inside the graph transaction under an advisory lock per object (`src/object_loop/formulas.py`).
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
| **integration-and-delivery** | Owns trains, promotion and delivery. Promotion and train policy are playbooks over its primitives; `INTEGRATION_LIFECYCLE_PLAYBOOK_IDS` re-points those activations to the route the integration layer chooses. `ci-main-sentinel` watches the main CI baseline. |
| **messaging-and-human-asks** | Playbooks send messages (`message_send`), open human gates and waits; spec approval through an ask emits `spec.approved`. Agent cron prompts are messages. |
| **supervisors-and-policy** | Supervisors receive what playbooks notice (failure triage, stuck tasks, object-loop refusals) and activate optional policy such as github-issue-triage. The supervisor patrol is an agent cron schedule. |
| **reviews-and-knowledge** | A playbook can be reviewed with its artifact hash pinned; `review.decided` and `human.review.completed` resume or trigger runs; spec approval starts ingestion. |
| **providers** | `provider-usage-probe` and `provider-failover` are the required policy over provider availability ([provider failover spec](../provider-failover.md)). |
| **configuration-and-vault** | Supplies `playbooks.*`, `security.capability_enforcement` and paths; the vault holds sources and installed bundles, written only if absent. |
| **api-and-clients** | Exposes playbook commands, the graph view and run inspection; the playbook commands reference is generated. |
| **dashboard** | Renders the playbook graph and run overlays from `graph_projection.py` and the pipeline view; it never runs policy. |
| **test-service-and-jobs** | Script steps submit durable managed jobs and resume from their recorded settlement; protected output is retrieved through owner-checked job logs. |
| **operations-and-recovery** | Doctor checks (`playbooks.required`, `reviewed_bundles`, `activation_stale`, `orphaned_runs`, …) and operator resolution of paused runs; `stuck-task-watch` and `supervisor-failure-triage` feed recovery. |
| **plugins-mcp-and-tools** | MCP tools are another front door to the same playbook commands; LLM steps call tools only through command contracts. |

## 5. Design principles and invariants

1. **Mechanism in code, policy in playbooks.** Scheduling never calls an LLM; judgment
   is a playbook calling commands. Compiling a playbook is pure; the semantic body comes
   from an author.
2. **What runs is a reviewed hash.** A run is pinned to an artifact's SHA-256 for its
   whole life; activation names the hash. Use deliberate import/activation rather than
   the pipeline `update-source` compatibility exception (section 6).
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

## 6. Known gaps and audit findings

These are implementation limits or mismatches observed at the audited revision;
this documentation audit does not change runtime behavior.

- **Source update bypasses normal activation.** For pipeline source,
  `playbook_commands.py` stores the compiled artifact and sets the existing activation
  hash/health directly. It does not call the activation-diff blocker path. Prose edits
  require a separate proposal and do not enqueue a compile task.
- **V2 lifecycle event schemas are missing.** `engine.py` emits
  `playbook.v2.run.started`, `playbook.v2.step.completed` and
  `playbook.v2.run.finished`, but `event_schemas.py` registers the older
  `playbook.run.*` names. A rule cannot validate a trigger on those emitted V2 names.
- **Legacy command descriptions disagree with handlers.** `resume_playbook` accepts
  a declared human option, not conversation text; `show_playbook_graph` delegates to
  structured graph projection, not ASCII/Mermaid rendering; `playbook_health` delegates
  to activation health, not run statistics. Their generated parameter/help descriptions
  still expose the old behavior. Handwritten command notes describe the current paths;
  fixing generated descriptions requires a source-contract change and regeneration.
- **Object-loop replay evidence predates its artifact.** The retained traces pin
  `e9a2e756…`; the current bundle pins `f83489a7…`. They must not be treated as
  acceptance of current bytes. The worker refusal in `object_loop_commands.py` also
  names nonexistent `aq object_loop inputs`; the real command is
  `aq task object-loop-inputs --project-id <project>`.
- **No dispatch cooldown, scope shadowing or concurrency cap.** Each event scans ready
  activations; a system and project activation both run. Dedupe covers one delivery id,
  not repeated emits of the same fact. Legacy engine comments still promise a removed
  `PlaybookManager` cooldown/cap; `max_concurrent_playbook_runs` does not cap V2 dispatch.
- **Timer comments are stale.** `timer_service.py` persists interval state and does not
  fire on boot, contrary to its module docstring.
- **Legacy compatibility is not a recovery guarantee.** `OrphanWorkflowRecovery` is
  not constructed by the orchestrator and references removed V1 queries. V2 runs use
  `RestartReconciler`; surviving legacy workflow rows need inspection.
- **Historical policy remains in the tree.** Three legacy bundles
  (`supervisor-digest`, `supervisor-hourly-report`, `blocked-task-escalation`) have no
  current source-builder entry. Retired integration ids remain as compatibility fences.
  Engine suppression of old default-pipeline rules and the retired `pr-merge-sweep`
  special case remain policy in mechanism. No automatic reflection playbook ships.

Installed-bundle drift is deliberate operator state, not a startup repair bug. Only
explicit doctor fixes or imports refresh it. Optional frontmatter `enabled: true`
does not install or activate a bundle; for example GitHub triage remains opt-in and
its commands are bound to the agent-queue repository.

## 7. Recent changes and historical proposals

The six-week history through 2026-10-22 includes these landed changes:

- V1 retirement and deliberate receipt-based installation (`3b6fb9578`, `1c0400b35`).
- Tool-free headless LLM execution, mandatory routing and agent cron; sleeping an
  instance now cancels its cron schedules (`49a6e9c1f`; later sleep cancellation).
- Reusable building blocks: generated command discovery, encrypted private values,
  collection expressions, nested loops, immutable observations, durable state and
  notification episodes, plus script steps backed by durable jobs. The F2 landing
  and F3 acceptance/handoff are present (`1be42a5e7`, `43b83dbdb`).
- Ordinary-process script execution with default storage roots and opt-in runtime
  digest manifests (`53d161b35`); private-value key provisioning (`36b3f6603`).
- Train policy controls (`1591e818f`) and release of pending dispatch claims on runtime
  shutdown (`ee36726bc`).

See [authoring and rollout](../../guides/playbook-building-blocks.md) for current
feature gates, limits, key setup and rollback. These features are no longer pending
refactor work, and the watch still does not join the initial installation set.
The old 2026-10-20 task-state snapshot has been removed: task states and vault review
statuses are not a durable description of shipped behavior.

The [V1 playbook design](../design/playbooks.md),
[sandboxed-playbook design](../design/sandboxed-playbooks.md),
[reflection spec](../reflection.md) and dated held-ingestion/shared-subgraph proposals
are historical records. The checked-in graph still has one trigger per rule and no
shared subgraph frames; `object-loop` therefore contains five rule-local sweeps.
Do not infer implementation or permission to activate from a proposal's approval.

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
