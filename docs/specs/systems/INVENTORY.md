# Agent Queue system inventory

**Status:** the approved system inventory for the system map (review
`rev-bright-rapids`, approved by the operator on 2026-10-20). One row per major
system. Every row now has its spec in this directory, and
[README.md](README.md) is the parent vision spec that consumes them. This file
decides *what the systems are* and *where each one's evidence lives*; it does
not describe behaviour. The slug column and each row heading link to the
system's spec.

**Inputs.** `docs/concepts/*.md` (current-behaviour pages),
`docs/reference/modules/<shard>.md` and
`docs/plans/documentation-overhaul/module-ownership.json` (the only complete
source-path to subject mapping), `docs/specs/README.md`,
`docs/specs/design/README.md`, `docs/superpowers/specs/README.md`,
`docs/guides/`, `docs/documentation-map.md`, and the open agent-queue task
graph as of 2026-10-20 (50 open tasks; `aq task list` is out of scope for a
worker token, so the list came from a read-only query of the same tables).
In-flight specs under review live in the operator vault at
`projects/agent-queue/specs/` and are cited by their dated file name.

**How to read a row.** *Slug* is the spec file name
(`docs/specs/systems/<slug>.md`). *Key source paths* are the packages the
module catalogue assigns to the subject; a package listed under one system is
not repeated under another. *Existing docs* are current pages first, then
historical specs that still describe the mechanism. *Open epics* are open
agent-queue tasks whose work lands in this system; a generated train or
promotion task (`repair-…`, `promote-…`) is not an epic and is not listed.
Each spec's planned-work section started from that column and re-checked
the open graph at writing time.

## Systems (18)

| # | Slug | System |
|---|---|---|
| 1 | [`daemon-core`](daemon-core.md) | Daemon core: composition root, orchestrator cycle, command handler, event bus |
| 2 | [`task-graph`](task-graph.md) | Task graph: hierarchy, state machine, readiness, formulas, deliverables, layout |
| 3 | [`scheduling-and-claims`](scheduling-and-claims.md) | Scheduling, pools, claims and resource gating |
| 4 | [`routing-and-profiles`](routing-and-profiles.md) | Routing, profiles and intelligence classes |
| 5 | [`sessions-and-harnesses`](sessions-and-harnesses.md) | Sessions, harnesses, prime, waits and hibernation, handoffs |
| 6 | [`projects-workspaces-git`](projects-workspaces-git.md) | Projects, workspaces, git and GitHub access |
| 7 | [`integration-and-delivery`](integration-and-delivery.md) | Integration train, git-first delivery, promotion and releases |
| 8 | [`playbooks`](playbooks.md) | Playbooks V2, agent cron and the object loop |
| 9 | [`messaging-and-human-asks`](messaging-and-human-asks.md) | Messaging, human asks, escalations, digest, Discord and reports |
| 10 | [`supervisors-and-policy`](supervisors-and-policy.md) | Supervisors, policy profiles and operator decisions |
| 11 | [`reviews-and-knowledge`](reviews-and-knowledge.md) | Document reviews, spec ingestion, knowledge records and memory |
| 12 | [`providers`](providers.md) | Providers, the LLM direct path, usage and failover |
| 13 | [`configuration-and-vault`](configuration-and-vault.md) | Configuration, secrets, tuning and the vault |
| 14 | [`api-and-clients`](api-and-clients.md) | HTTP/WebSocket API and generated clients |
| 15 | [`dashboard`](dashboard.md) | Dashboard, dashboard server and panes |
| 16 | [`test-service-and-jobs`](test-service-and-jobs.md) | Test service, managed jobs and test selection |
| 17 | [`operations-and-recovery`](operations-and-recovery.md) | Operations: doctor, metrics, install, deploy, backup and recovery |
| 18 | [`plugins-mcp-and-tools`](plugins-mcp-and-tools.md) | Plugins, MCP, tool catalogue and agent skills |

Persistence is deliberately not a system of its own: `src/database/` and
`migrations/` are the storage every system shares, documented by
`docs/reference/database/` and `docs/guides/migrations.md`. The `daemon-core`
spec owns the schema and migration invariants; every other spec names its own
tables.

## Inventory

### 1. [`daemon-core`](daemon-core.md) — composition root, orchestrator cycle, command handler, event bus

**Purpose.** The API-only daemon process on port 8081: `src/main.py` builds the
database engine, the command handler, the orchestrator and the services, then
runs the scheduler cycle on a timer. Every state change from the CLI, MCP, the
API, Discord and playbooks goes through one `CommandHandler` whose commands
return `{"success": bool, ...}` dicts and are described by contracts in
`src/commands/contracts/builtin.py`. The orchestrator cycle is deterministic
and makes no LLM calls; it emits typed events on the event bus that playbooks
and dashboards subscribe to. The daemon owns the PostgreSQL schema and its
Alembic migration guard. `src/runtimes/` is an injection seam that registers
nothing: there is no in-process agent runtime.

**Key source paths.** `src/main.py`, `src/orchestrator/core.py`,
`src/orchestrator/context.py`, `src/orchestrator/events.py`,
`src/orchestrator/execution.py`, `src/orchestrator/monitoring.py`,
`src/commands/` (handler and mixins, `contracts/`), `src/event_bus.py`,
`src/event_schemas.py`, `src/services/`, `src/runtimes/`, `src/daemon_state.py`,
`src/surface_schema.py`, `src/database/` (tables, engine, migration guard,
queries), `src/models.py`, `migrations/`.

**Existing docs.** `docs/concepts/architecture.md`,
`docs/reference/modules/architecture.md`, `docs/reference/modules/database.md`,
`docs/reference/database/`, `docs/specs/main.md`, `docs/specs/orchestrator.md`,
`docs/specs/command-handler.md`, `docs/specs/event-bus.md`,
`docs/specs/models-and-state-machine.md`, `docs/specs/database.md`,
`docs/specs/design/aq-surface.md`, `docs/specs/implementation/aq-surface.md`,
`docs/specs/design/event-loop-responsiveness.md`,
`docs/specs/design/guiding-design-principles.md`,
`docs/specs/design/trust-and-ops.md`, `docs/guides/migrations.md`,
`docs/superpowers/specs/2026-09-07-sqlite-removal-implementation.md`,
`profile.md`, `docs/contributing/repo-map.md`.

**Open epics.** None directly. Items from `stark-willow-95` touch the cycle
(`.7` replaces the timed train window with idle/busy dispatch); see system 7.

### 2. [`task-graph`](task-graph.md) — hierarchy, state machine, readiness, formulas, deliverables, layout

**Purpose.** The durable work graph: tasks with parent/child hierarchy, phases,
subtasks, `needs:` dependencies, barriers and formulas that expand into generated
subgraphs; the task state machine (DEFINED, READY, IN_PROGRESS, BLOCKED, PAUSED,
COMPLETED and the rest) and the readiness rules that compute the frontier;
deliverables (tests, commands, reviews) that the close gate checks; task change
sets staged as one proposal; `aq task explain` and the task names, summaries and
server-side spatial layout the dashboard renders. This is the data model every
other system reads; it owns no agents.

**Key source paths.** `src/task_graph/` (including `layout/`),
`src/state_machine.py`, `src/deliverables.py`, `src/explain.py`,
`src/task_names.py`, `src/task_summary.py`, `src/review_keys.py`,
`src/database/queries/hierarchy_queries.py`, `src/commands/phase_commands.py`,
`src/commands/task_subtask_commands.py`, `src/api/graph_layout.py`.

**Existing docs.** `docs/concepts/tasks.md`, `docs/reference/modules/tasks.md`,
`docs/specs/design/work-graph.md`, `docs/specs/implementation/work-graph.md`,
`docs/specs/design/formulas.md`, `docs/specs/design/generated-task-templates.md`,
`docs/specs/design/feature-pauses.md`, `docs/specs/implementation/feature-pauses.md`,
`docs/specs/design/task-completion-ci.md`,
`docs/specs/design/review-revision-completion.md`,
`docs/specs/models-and-state-machine.md`, `docs/specs/plan-parser.md`,
`docs/specs/npm-workspace-test-deliverables.md`,
`docs/guides/task-change-sets.md`, `docs/guides/generated-task-templates.md`,
`docs/guides/superseded-completion.md`,
`docs/superpowers/specs/2026-08-28-swarm-work-model-design.md`,
`docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md`,
`docs/superpowers/specs/2026-09-08-task-deletion-with-materialized-branches-design.md`,
vault `2026-10-13-spec-ingestion-with-task-structure-review.md`,
vault `2026-10-06-task-graph-ui-redesign-2.md`.

**Open epics.** `solid-flare-36` (the first system-map epic, since closed;
`eager-vault-55` wrote the per-system specs and the parent vision spec). `stark-willow-95.10` (epic checks become
advisory) and `.14` (retained prerequisite stacks by default) change readiness
and close rules; see system 7 for the epic.

### 3. [`scheduling-and-claims`](scheduling-and-claims.md) — scheduling, pools, claims and resource gating

**Purpose.** How ready work becomes a running agent without an LLM in the loop:
the frontier query, global worker pools and their sizing, the claim path
(`aq task claim --next`) with claim preparation, identity rechecks, the
`.aq/claim.json` record and claim timing, pool fairness across projects, the
timer service and recurring schedule, and the box-wide resource gates (test
slots, worker caps, usage-aware concurrency) that keep the machine responsive.
Stranded-work and route-needed sweeps return orphaned claims to the frontier.

**Key source paths.** `src/orchestrator/pools.py`, `src/pool_claims.py`,
`src/claim_preparation.py`, `src/claim_timing.py`,
`src/preparation_failure.py`, `src/database/queries/claim_queries.py`,
`src/commands/claim_commands.py`, `src/orchestrator/{layout_step,merge_slot,
route_needed,stranded_work,task_checkpoint,triage,sync_workflow,pr_polling}.py`,
`src/resources/`, `src/schedule.py`, `src/scheduler.py`,
`src/timer_service.py`, `src/orchestrator/agent_reconciler.py`.

**Existing docs.** `docs/concepts/scheduling.md`,
`docs/reference/modules/scheduler.md`, `docs/guides/worker-pools.md`,
`docs/guides/resource-gating.md`, `docs/guides/default-tuning.md`,
`docs/guides/e2e-swarm.md`, `docs/specs/scheduler-and-budget.md`,
`docs/specs/schedule.md`, `docs/specs/pool-display-names.md`,
`docs/specs/design/shared-pool-fairness.md`,
`docs/specs/design/hybrid-workspace-pools.md`,
`docs/specs/design/agent-coordination.md`,
`docs/superpowers/specs/2026-09-08-global-worker-pools-design.md`,
`docs/superpowers/specs/2026-08-24-usage-aware-concurrency.md`,
`docs/superpowers/specs/2026-08-28-swarm-work-model-design.md`,
`docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md`,
vault `2026-10-13-canonical-origin-admission-and-expired-reservation-lease.md`,
vault `2026-10-13-paused-acceptance-task-resumes.md`,
vault plans `2026-09-27-provider-worker-allocation-controls.md`.

**Open epics.** `amber-cascade-54` (bugfix, IN_PROGRESS: claim-preparation
instance handoff regression blocking staging). `stark-willow-95.13` (one
admission view for frontier, pools explain and delivery guards) and `.11`
(warm workspaces, shared compiler cache) touch claim preparation.

### 4. [`routing-and-profiles`](routing-and-profiles.md) — routing, profiles and intelligence classes

**Purpose.** Which agent definition runs a task. Profiles are global markdown
files in the vault; worker rungs are derived per intelligence class and
installed harness from the `worker-<harness>` template, never authored.
Intelligence classes map a class name to a concrete model per provider. The
router (the shipped `default-assignment-routing` playbook plus the routing
service) chooses a profile from task kind, class hint, provider availability,
risk and repository capacity, records the route and reroutes on failover;
operators may pin one task with a route override. Capability grants on the
supervisor and worker templates are merged additively on every start.

**Key source paths.** `src/routing/`, `src/profiles/` (catalog,
capability sync, workspace kinds, MCP grants), `src/intelligence_classes/`,
`src/agents/`, `src/agent_names.py`, `src/assignment_routing.py`,
`src/commands/routing_commands.py`, `src/doctor/routing_checks.py`,
`src/prompts/default_playbooks/default-assignment-routing.md`,
`src/prompts/default_intelligence_classes/`, the shipped profiles seeded by `src/vault.py`.

**Existing docs.** `docs/concepts/agents-and-routing.md`,
`docs/reference/modules/routing.md`, `docs/reference/profiles-and-classes.md`,
`docs/guides/routine-routing-preference.md`,
`docs/guides/benchmark-routing-export.md`, `docs/guides/worker-pools.md` §7b,
`docs/specs/agent-profiles.md`, `docs/specs/design/profiles.md`,
`docs/specs/design/routing-live-context.md`,
`docs/specs/task-reroute-recovery.md`,
`docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md`,
`docs/superpowers/specs/2026-08-31-playbook-intelligence-routing-design.md`,
`docs/superpowers/specs/2026-04-25-profile-validation-design.md`,
vault `2026-10-09-risk-aware-routing` (two documents),
vault `2026-10-10-routable-operator-workers.md`.

**Open epics.** `clear-impact-72` child for `default-assignment-routing`
(rewrite evaluation; see system 8). No routing-owned epic is open; the
2026-10-19 commit `d46334b71` (repository capacity and strict repair
preferences) is the latest landed change.

### 5. [`sessions-and-harnesses`](sessions-and-harnesses.md) — sessions, harnesses, prime, waits and hibernation, handoffs

**Purpose.** Every agent is a tmux session started from a harness description
(`claude`, `codex`, `gemini`): the session spec and environment scrub, the
provider that spawns and resumes the process, readiness detection, pane
broadcasting and terminal PTY reconnection, the flock registration that every
session needs before spawn, the session reconciler and desired state. `aq
prime` renders the task, role, rules and deliverables into the session. Agents
hibernate on durable waits (task, review, CI, time) and are woken by events
rather than polling; handoff notes carry context across restarts and
compaction; the stall watch and question routing surface a session that has
gone quiet or asked something. Collaboration lets sessions address each
other.

**Key source paths.** `src/sessions/` (tmux, provider, spec, env, reconciler,
harness modules, transcripts, pane broadcaster, terminal PTY, questions,
`default_harnesses/*.md`), `src/panes/`, `src/claim_file.py`,
`src/env_scrub.py`, `src/prime/`, `src/waits/`, `src/wait_runtime/`,
`src/agent_waits.py`, `src/handoffs.py`, `src/stall_watch.py`,
`src/collaboration.py`.

**Existing docs.** `docs/concepts/sessions.md`,
`docs/reference/modules/sessions.md`, `docs/reference/harnesses.md`,
`docs/guides/agent-waits.md`, `docs/guides/wake-context-compaction.md`,
`docs/guides/flock-sessions.md`, `docs/guides/session-troubleshooting.md`,
`docs/guides/agent-collaboration.md`, `docs/specs/design/session-runtime.md`,
`docs/specs/implementation/session-runtime.md`,
`docs/specs/design/worktree-execution.md`, `docs/specs/worker-context.md`,
`docs/specs/terminal-reconnection.md`, `docs/specs/stuck-task-watch.md`,
`docs/specs/operator-stall-sweep.md`, `docs/specs/operations/session-cleanup.md`,
`docs/specs/runtimes/` (historical; no runtime ships),
`docs/superpowers/specs/2026-08-27-session-desired-state-design.md`,
`docs/superpowers/specs/2026-08-30-agent-flock-design.md`,
`docs/superpowers/specs/2026-08-30-agent-question-routing.md`,
`docs/superpowers/specs/2026-08-25-live-pane-streaming-design.md`,
vault `2026-10-13-event-driven-agent-hibernation-and-verified-waits.md` and
its implementation spec, vault `2026-10-07-missed-supervisor-message-wake-up-rca.md`.

**Open epics.** `bold-zenith-11` (IN_PROGRESS, 33 children, 1 open:
`.22` flip the defaults, refuse v1 bodies, ship the doctor checks and the
runbook) implements event-driven hibernation and verified waits.

### 6. [`projects-workspaces-git`](projects-workspaces-git.md) — projects, workspaces, git and GitHub access

**Purpose.** A project is a repository plus policy; a workspace is where a
session works: worktree slots, native checkouts, retained objects and
attachments, claimed and recovered per task. `GitManager` is the one async git
surface (worktrees, fetch, push, PR identity, askpass, CI gate), and all GitHub
operations go through one `gh`-backed path with the daemon-injected git
identity. Workspace specs are watched from the vault; retained-object integrity
and git-corruption detection protect the shared object store.

**Key source paths.** `src/projects/`, `src/git/` (manager, GitHub, CI gate,
askpass), `src/orchestrator/{worktree_manager,workspace,
workspace_claim_recovery,workspace_attachments,git_ops,base_workspace}.py`,
`src/profiles/workspace_kind_*.py`, `src/workspace_spec_watcher.py`,
`src/workspace_names.py`.

**Existing docs.** `docs/concepts/projects-and-workspaces.md`,
`docs/reference/modules/workspaces.md`, `docs/guides/project-onboarding.md`,
`docs/guides/retained-git-corruption.md`, `docs/specs/git.md`,
`docs/specs/git-identity.md`, `docs/specs/github-access.md` (in flight,
`smart-beacon`), `docs/specs/design/workspaces-v2.md`,
`docs/specs/design/worktree-execution.md`,
`docs/specs/implementation/worktree-execution.md`,
`docs/specs/design/retained-object-integrity.md`,
`docs/specs/design/branch-tracking-deployment.md`,
`docs/specs/design/git-completion-provenance.md`,
`docs/plans/github-access/`, `docs/superpowers/specs/2026-09-03-project-onboarding-design.md`,
vault `2026-10-14-retained-git-corruption-detection-defects.md`,
vault `2026-10-06-distributed-worker-nodes.md`,
vault `2026-10-06-containerized-sandboxed-team-hosted-agent-queue.md`.

**Open epics.** None directly. `stark-willow-95.11` (warm workspaces and a
shared compiler cache) and `.12` (native-storage relocation tooling) land here.

### 7. [`integration-and-delivery`](integration-and-delivery.md) — integration train, git-first delivery, promotion and releases

**Purpose.** How a worker's branch reaches `dev`, `staging` and `main` without
a human merge. The integration service runs trains per subject (task, epic,
root), builds candidates, runs CI, repairs red batches with delegated fixers,
settles required jobs, reconciles retained branches and cleans them up. The
development publisher owns the generated-file merge rule. Promotion flows move
one tested commit through a branch chain under a PR each; releases publish a
promotion commit under an annotated tag that `aq update` deploys. Delivery
truth (what actually reached which branch) is recorded and diagnosed here.

**Key source paths.** `src/integration/` (service, engine, scheduler,
reconciler, candidates, CI, repair, promotion, main promotion, release,
records, subjects, gates, cleanup, development, root and parent handling),
`src/integrations/`, `src/commands/integration_commands.py`,
`src/commands/ci_commands.py`, `src/cli/integration.py`,
`src/doctor/integration_checks.py`, `src/releases/`.

**Existing docs.** `docs/concepts/integration.md`,
`docs/reference/modules/integration.md`, `docs/guides/development-integration.md`,
`docs/guides/hierarchical-integration-trains.md`, `docs/guides/git-first-train-runbook.md`,
`docs/guides/git-first-backlog-settlement.md`, `docs/guides/git-first-cutover-runbook.md`,
`docs/guides/integration-migration.md`, `docs/guides/integration-troubleshooting.md`,
`docs/guides/integration-ci-boundaries.md`, `docs/guides/promotion-flow.md`,
`docs/guides/releases.md`, `docs/guides/reconciler-shadow-cutover-runbook.md`,
`docs/specs/design/development-integration.md`,
`docs/specs/design/development-conflict-repair.md`,
`docs/specs/design/batch-conflict-repair-starts.md`,
`docs/specs/design/conflicting-source-batches.md`,
`docs/specs/design/promotion-flow.md`, `docs/specs/design/manual-integration.md`,
`docs/specs/design/required-job-train-settlement.md`,
`docs/specs/design/root-pr-check-recovery.md`,
`docs/specs/design/epic-aggregate-reverification.md`,
`docs/specs/design/integration-identity-diagnostics.md`,
`docs/specs/design/integration-repository-recovery.md`,
`docs/specs/design/branch-retirement.md`,
`docs/specs/design/completed-container-checkpoint-recovery.md`,
`docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md`
and its review, `docs/superpowers/specs/2026-09-05-ci-main-sentinel-design.md`,
vault `2026-10-12-fast-smooth-integration.md` (rev 2 approved; rev 3 and the
2026-10-20 rev 4 retrospective decisions), vault
`2026-10-04-git-first-integration-train.md` and refresh, vault
`2026-10-05-dev-branch-releases.md` (rev 1 to 3), vault
`2026-10-06-git-first-train-fidelity.md` and the e5a-b13 release docs set,
vault `2026-10-19-retrospective-why-work-stalls-before-reaching-dev.md`,
vault plans `2026-09-23-epic-prs-and-integration-train.md`,
`2026-09-27-development-publisher-delivery-truth.md`, `train-refactor-2026-10-02.md`.

**Open epics.** `stark-willow-95` (IN_PROGRESS, 22 children, 15 open) is the
fast smooth integration implementation: `.3` redrive stuck delivery, `.4`
completion-to-dev metrics, `.5` delivery median and p90 view, `.7` idle/busy
train dispatch, `.8` group red batch failures, `.9` parallel fixers, `.10`
advisory epic checks, `.11` warm workspaces, `.12` native-storage relocation,
`.13` one admission view, `.14` retained prerequisite stacks, `.15` author
context through the fenced parent merge, `.16` speculative next-batch CI,
`.17` align shipped policy profiles, `.18` validate flow and cutover
manifests. Generated train tasks open today: two repair tasks and one
`promote staging 22cffd1b7236` task.

### 8. [`playbooks`](playbooks.md) — Playbooks V2, agent cron and the object loop

**Purpose.** Policy lives in playbooks, mechanism in code. A V2 playbook is a
vault markdown source compiled with a semantic body of `rules` and `steps`
into a validated artifact; the engine runs it on event-bus events through
executors (command, LLM, agent task, wait, terminal, foreach, decision), with
proposals, human gates and activation state. Shipped playbooks are installed
deliberately per project from reviewed bundles; the pipeline playbook ingests
specs; reusable building blocks (shared rules, subgraphs, scripts) are the
refactor under way. Agent cron schedules recurring prompt wake-ups; the object
loop is a reviewed generation policy built on formulas.

**Key source paths.** `src/playbooks/` (engine, runtime, executors,
activation, artifacts, proposal, validation, waits), `src/playbook_schema.json`,
`src/playbook_v2_schema.json`, `src/prompts/default_playbooks/`,
`src/prompts/reviewed_playbooks/`, `src/prompts/project_playbooks/`, `src/prompts/integration_playbooks/`, `src/prompts/task_templates/`, `src/prompts/formulas/`,
`src/orphan_workflow_recovery.py`, `src/workflow_pipeline_view.py`,
`src/workflow_stage_resume_handler.py`, `src/agent_cron.py`,
`src/object_loop/`, `tests/fixtures/playbooks/v2/` (reviewed bundle sources).

**Existing docs.** `docs/concepts/playbooks.md`,
`docs/reference/modules/playbooks.md`, `docs/reference/playbook-commands/`,
`docs/guides/agent-cron.md`, `docs/guides/object-loop.md`,
`docs/guides/github-issue-triage.md`, `docs/specs/design/playbooks.md`,
`docs/specs/design/sandboxed-playbooks.md`, `docs/specs/design/agent-cron.md`,
`docs/specs/design/object-script-loop.md`, `docs/specs/reflection.md`,
`docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md`,
`docs/superpowers/specs/2026-09-04-playbook-v2-fresh-cutover-design.md`,
`docs/superpowers/specs/2026-08-31-mandatory-triage-playbook-design.md`,
`docs/superpowers/specs/2026-09-06-blocked-task-escalation-design.md`,
vault `2026-10-12-implementation-playbooks-reusable-building-blocks.md`,
vault `2026-10-12-playbooks-can-do-anything.md`,
vault `2026-10-14-playbook-scripts-no-sandbox-decision.md`,
vault `2026-10-11-deliberate-playbook-installation-and-project-policy.md`,
vault plans `2026-10-11-playbook-graph-shared-rules-subgraphs.md`.

**Open epics.** `clear-impact-72` (IN_PROGRESS, 11 plan children, one per
shipped playbook: ci-main-sentinel, provider-usage-probe,
supervisor-failure-triage, default-assignment-routing, default-pipeline,
github-issue-triage, morning-report, object-loop, promotion-continuous,
promotion-request, provider-failover). `wise-orbit-24` (BLOCKED: refactor F3
acceptance, migration and rollback rehearsal, activation handoff).
`agile-vault-38` (BLOCKED bugfix: playbook scripts refused by default,
runtime/manifest/input/state roots). `agile-stone-80` (READY: merge
playbooks F2 stuck-task watch into the refactor epic and land on dev).

### 9. [`messaging-and-human-asks`](messaging-and-human-asks.md) — messaging, human asks, escalations, digest, Discord and reports

**Purpose.** Every message between agents, the daemon and Jack: the message
store and inbox, conversations and threads, the delivery engine that wakes idle
sessions, notifications, the single Discord channel, remote links, human asks
(one place to ask Jack, with buttons, authority, batch scope and ingestion of
the answer), escalations with delivery policy, the deterministic digest, and the
morning and hourly reports. Supervisor-to-worker mail rides the same rails.

**Key source paths.** `src/messages/`, `src/messaging/`,
`src/conversations/`, `src/delivery/`, `src/notifications/`, `src/discord/`,
`src/asks/`, `src/escalations/`, `src/digest/`, `src/reports/`,
`src/remote_links.py`, `src/dashboard_paths.py`,
`src/commands/{message,escalation,digest,discord}_commands.py`.

**Existing docs.** `docs/concepts/messaging.md`,
`docs/reference/modules/communications.md`, `docs/guides/escalations.md`,
`docs/guides/discord-conversations.md`, `docs/guides/human-asks-rollout.md`,
`docs/guides/supervisor-hourly-reports.md`, `docs/specs/escalation-delivery.md`,
`docs/specs/design/messaging-rework.md`, `docs/specs/implementation/messaging-rework.md`,
`docs/specs/design/human-ask-cutover-status.md`,
`docs/specs/design/human-ask-legacy-reset.md`, `docs/specs/messaging/`
(superseded), `docs/superpowers/specs/2026-09-08-discord-simplification-implementation.md`
(byte-identical mirror in the vault), vault `2026-10-11-one-place-to-ask-jack`
(design and implementation).

**Open epics.** `calm-grove-14` (READY: H09, remove duplicate human
publishers and reconcile repository policy, after `stark-forge-10` lands).
`vivid-pinnacle-60` (PAUSED, operator: H08-OPS record authorized cutover and
observe the 30-day compatibility exit). `vivid-rapids-36` (READY, operator:
H09-OPS verify installed cleanup, vault mirror, policy and final live
acceptance).

### 10. [`supervisors-and-policy`](supervisors-and-policy.md) — supervisors, policy profiles and operator decisions

**Purpose.** The project supervisor is a long-lived session (and a projectless
global supervisor) that triages failures, answers workers, files change sets
and holds delegated decisions; it is restarted and re-adopted across daemon
updates. Policy profiles are the named bundles of integration, routing and
pipeline policy a project selects, archived and versioned; operator decisions
record the human choices the daemon must not make on its own. The factory
policy document is the normative statement of how work is admitted, delivered
and recovered.

**Key source paths.** `src/policy_profiles/` (archive, builtin, integration,
service), `src/operator_decisions.py`, `src/commands/supervisor_authority.py`, `src/commands/agent_commands.py`,
`src/prompts/supervisor_system.md`, the supervisor template seeded by `src/vault.py`, the
`supervisor-failure-triage` and `morning-report` shipped playbooks.

**Existing docs.** `docs/concepts/factory-policy.md`,
`docs/specs/supervisor.md`, `docs/specs/design/supervisor-agent.md`,
`docs/specs/implementation/supervisor-agent.md`,
`docs/specs/design/policy-profiles.md`, `docs/specs/design/operator-decisions.md`,
`docs/specs/design/trust-and-ops.md`, `docs/specs/implementation/trust-and-ops.md`,
`docs/guides/task-change-sets.md`, `docs/guides/supervisor-hourly-reports.md`,
`docs/superpowers/specs/2026-08-30-projectless-supervisor-design.md`,
`docs/superpowers/specs/2026-08-30-llm-direct-path-design.md` (the
`Supervisor.chat()` replacement), vault `2026-10-07-missed-supervisor-message-wake-up-rca.md`.

**Open epics.** `stark-willow-95.17` (align shipped policy profiles) and
`.18` (validate flow and cutover manifests). The supervisor-failure-triage
and morning-report rewrites sit under `clear-impact-72`.

### 11. [`reviews-and-knowledge`](reviews-and-knowledge.md) — document reviews, spec ingestion, knowledge records and memory

**Purpose.** Documents that are reviewed rather than committed: specs, plans,
proposals and reports submitted as revisions, mirrored to the vault, dispatched
for adversarial review and decided by Jack; the `--after-review` gate on filed
work; held ingestion that turns an approved spec into a task graph after a
fidelity review and release. Knowledge records are versioned, cited, searchable
findings with authority, deprecation and sharing rules; the work-and-knowledge
record store, export, backfill and identity sit beside them. Memory
consolidation and reflection are the older scoped-memory layer, with the
`aq-memory` plugin extracted.

**Key source paths.** `src/reviews/`, `src/records/` (artifacts, auth,
backfill, export, graph, identity, outbox, search, service), `src/knowledge/`
(authority, budget, capture, context, delivery, deprecation, extraction,
proposals, search, sharing, imports), `src/prompts/memory_*.py`,
`src/commands/review_commands.py`, the `default-pipeline` shipped playbook,
`src/skills/aq-reviews/`, `src/skills/aq-knowledge/`.

**Existing docs.** `docs/guides/reviews.md`, `docs/guides/knowledge-records.md`,
`docs/guides/knowledge-deprecation.md`, `docs/guides/knowledge-migration-restore.md`,
`docs/guides/mini-projects.md`, `docs/specs/knowledge-context.md`,
`docs/specs/memory-consolidation.md`, `docs/specs/reflection.md`,
`docs/specs/design/memory-plugin.md`, `docs/specs/design/memory-scoping.md`,
`docs/specs/design/knowledge-deprecation.md`,
`docs/specs/design/knowledge-protection.md`,
`docs/specs/design/self-improvement.md`, `docs/specs/implementation/spec-ingestion.md`,
`docs/superpowers/specs/2026-04-25-aq-memory-extraction-design.md`,
vault `2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-release.md`,
vault `2026-10-13-spec-ingestion-with-task-structure-review.md`,
vault `2026-10-03-knowledge-records-migration-restore.md`,
vault plans `2026-09-21-document-review.md`, `2026-10-01-work-and-knowledge-records.md`.

**Open epics.** `sharp-cascade-53` (IN_PROGRESS: held ingestion P3, release
and recovery), `nimble-lantern-37` (IN_PROGRESS: held ingestion P5, policy,
profiles, pipeline, docs; children `.2` spec-ingest profile text and
default-pipeline rules, `.3` docs), `calm-orbit-69` (IN_PROGRESS: held
ingestion P6, API, dashboard, CLI; child `.3` CLI `aq batch`, doctor checks,
MCP tools).

### 12. [`providers`](providers.md) — providers, the LLM direct path, usage and failover

**Purpose.** The model providers behind the harnesses (Anthropic, OpenAI,
Google): availability states and their evidence, the usage probe and token
accounting that feed usage-aware concurrency, the LLM direct path and its
logging for the few daemon-side model calls, and provider failover as a
reviewed playbook that reroutes work across families when a provider is out.

**Key source paths.** `src/providers/`, `src/llm/`, `src/tokens/`,
`src/llm_logger.py`, `src/orchestrator/provider_failover.py`, the
`provider-failover` and `provider-usage-probe` shipped playbooks.

**Existing docs.** `docs/concepts/providers.md`,
`docs/reference/modules/providers.md`, `docs/guides/provider-outage.md`,
`docs/guides/llm-providers.md`, `docs/specs/provider-failover.md` (in flight,
`bold-rapids`), `docs/specs/llm-logging.md`,
`docs/superpowers/specs/2026-09-07-provider-usage-design.md` and its
implementation spec, `docs/superpowers/specs/2026-08-30-llm-direct-path-design.md`,
`docs/superpowers/specs/2026-08-24-usage-aware-concurrency.md`.

**Open epics.** None directly; `provider-failover` and `provider-usage-probe`
rewrites sit under `clear-impact-72`.

### 13. [`configuration-and-vault`](configuration-and-vault.md) — configuration, secrets, tuning and the vault

**Purpose.** Operator config in `~/.agent-queue/config.yaml` with its schema,
editor, secrets and default tuning; portable config for moving an install; and
the vault, the markdown tree of playbooks, profiles, memory, specs and
knowledge that the daemon watches, indexes, seeds write-if-absent and
cross-links with wiki links. Prompt building and the prompt manager assemble
agent prompts from vault content.

**Key source paths.** `src/config.py`, `src/config_editor.py`,
`src/config_secrets.py`, `src/config_tuning.py`, `src/portable_config.py`,
`src/vault.py`, `src/vault_manager.py`, `src/vault_watcher.py`,
`src/vault_index.py`, `src/vault_glossary.py`, `src/wiki_links.py`,
`src/file_watcher.py`, `src/facts_handler.py`, `src/facts_parser.py`,
`src/override_handler.py`, `src/readme_handler.py`,
`src/reference_stub_enricher.py`, `src/prompt_builder.py`,
`src/prompt_manager.py`, `src/prompts/` (shipped content).

**Existing docs.** `docs/concepts/configuration-and-vault.md`,
`docs/reference/configuration.md`, `docs/reference/modules/vault.md`,
`docs/guides/default-tuning.md`, `docs/specs/config.md`,
`docs/specs/portable-config.md`, `docs/specs/prompt-builder.md`,
`docs/specs/design/vault.md`.

**Open epics.** None.

### 14. [`api-and-clients`](api-and-clients.md) — HTTP/WebSocket API and generated clients

**Purpose.** The daemon's FastAPI surface on port 8081 (routers, typed models,
WebSocket streams, `openapi.json`) and the two generated clients, Python
`packages/aq-client` and TypeScript `packages/aq-ts-client`, regenerated by
script and never hand-edited. The API is the only way the dashboard server,
MCP and remote tooling reach the daemon.

**Key source paths.** `src/api/` (`app.py`, `spec.py`, `models/`, `routers/`),
`openapi.json`, `packages/aq-client/`, `packages/aq-ts-client/`,
`scripts/regenerate-api-client.sh`, `scripts/regenerate-ts-client.sh`,
`scripts/regenerate-generated.sh`.

**Existing docs.** `docs/reference/api/`, `docs/reference/modules/api.md`,
`docs/contributing/codegen.md`, `docs/specs/mcp-server.md`,
`docs/specs/tiered-tools.md`.

**Open epics.** `calm-orbit-69` (held ingestion P6: API, dashboard, CLI) adds
endpoints; see system 11.

### 15. [`dashboard`](dashboard.md) — dashboard, dashboard server and panes

**Purpose.** The React browser workspace (command center, task graph, agents
console, reviews, settings) served by the dashboard server on port 8082, a
separate process that proxies the API, owns typed server-backed state
namespaces and the editor, and renders pane views (task detail, session peek,
diff review, playbook run inspector) that the pane plugin interface defines.

**Key source paths.** `dashboard/` (258 modules; `dashboard/AGENTS.md` adds
its rules), `src/dashboard_server/`, `src/dashboard_state/`, `src/editor/`,
`dashboard/src/pages/command-center/layout-v2/`.

**Existing docs.** `docs/guides/dashboard.md`,
`docs/reference/modules/dashboard.md`, `docs/specs/dashboard-server.md`,
`docs/superpowers/specs/2026-08-21-dashboard-v2-and-work-pipeline-design.md`,
`docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md`, the
`2026-08-22-pane-*` designs, `docs/superpowers/specs/2026-08-30-command-center-unification-design.md`,
`docs/superpowers/specs/2026-09-10-dashboard-state-contract-design.md`,
`docs/superpowers/specs/2026-09-23-dashboard-performance.md` and the
2026-09-04 investigation and plan,
`docs/superpowers/specs/2026-09-07-project-folders-and-drag-drop-design.md`,
`docs/superpowers/specs/2026-09-07-recent-work-and-model-attribution-design.md`,
vault `2026-10-03-dashboard-feedback-and-versioned-releases.md`,
vault `2026-10-06-task-graph-ui-redesign-2.md`, vault plans
`2026-09-25-dashboard-performance.md`, `2026-09-26-mobile-dashboard.md`.

**Open epics.** `stark-willow-95.5` (delivery median and p90 view) and
`calm-orbit-69` (held-batch dashboard surface).

### 16. [`test-service-and-jobs`](test-service-and-jobs.md) — test service, managed jobs and test selection

**Purpose.** `aq test` and the managed job runner: box-wide test slots, the
per-session worker cap, marker deselects, pytest output capture, job
artifacts and policy, the e2e kit and workspace jobs; smart test selection
builds the selection catalogue from module ownership and proposes a plan in
shadow mode. CI in GitHub Actions consumes the same selection rules and
baselines.

**Key source paths.** `src/jobs/` (runner, service, artifacts, e2e, matter,
policy, workspace, pytest output), `src/test_selection/`,
`tests/selection_catalogue.json`, `tests/selection_areas.yaml`,
`tests/selection_rules.yaml`, `scripts/generate-selection-catalogue.py`,
`scripts/e2e-env.sh`, `scripts/e2e-smoke.sh`, `.github/workflows/`.

**Existing docs.** `docs/guides/resource-gating.md`,
`docs/guides/smart-test-selection.md`, `docs/guides/e2e-swarm.md`,
`docs/contributing/testing.md`, `docs/contributing/ci.md`,
`docs/specs/implementation/managed-jobs.md`,
`docs/specs/design/task-completion-ci.md`, vault plans
`2026-09-25-smart-test-selection.md`, `2026-10-07-bounded-aq-checks.md`,
`docs/superpowers/specs/2026-09-01-dashboard-vitest-flakiness.md`.

**Open epics.** `stark-willow-95.16` (speculative next-batch CI) and `.8`
(group red batch failures) consume job results; see system 7.

### 17. [`operations-and-recovery`](operations-and-recovery.md) — doctor, metrics, install, deploy, backup and recovery

**Purpose.** Everything an operator runs that is not a task: `aq doctor` and
its check modules with `--fix`, fleet metrics and benchmarks, the installer
and setup wizard, service and watchdog units, `aq update` tag deployment with
`deploy.json`, `aq restart` that preserves sessions, PostgreSQL provisioning,
backup and restore, logging configuration, and the recovery runbooks for
stranded work, orphaned workflows and corrupted retained objects.

**Key source paths.** `src/doctor/` (22 check modules), `src/metrics/`,
`src/benchmark/`, `src/install/` (deploy, update, wizard, postgres, pools,
providers, service, watchdog), `src/logging_config.py`, `src/cli/` (the
operator command surface), `src/commands/` operator mixins, `scripts/`.

**Existing docs.** `docs/guides/operations.md`,
`docs/reference/modules/operations.md`, `docs/reference/modules/cli.md`,
`docs/reference/cli/`, `docs/tutorials/install.md`,
`docs/contributing/installer-testing.md`, `docs/guides/releases.md`
(deploy and restore halves), `docs/specs/setup-wizard.md`,
`docs/specs/packaging.md`, `docs/specs/design/fleet-metrics.md`,
`docs/specs/design/branch-tracking-deployment.md`,
`docs/plans/install-onboarding/`, `docs/superpowers/specs/2026-09-06-release-roadmap.md`,
vault plans `2026-09-21-integration-owner-recovery.md`.

**Open epics.** `bold-zenith-11.22` ships doctor checks and a runbook;
`calm-orbit-69.3` adds doctor checks for held batches. No operations-owned
epic is open.

### 18. [`plugins-mcp-and-tools`](plugins-mcp-and-tools.md) — plugins, MCP, tool catalogue and agent skills

**Purpose.** The two extension directions: internal plugins that add
capabilities to the daemon (files, git, inbox, notes, vibecop) and the external
`aq-memory` plugin; MCP servers an agent may use, registered per profile with
tiered tools and `aq://` URIs, including the embedded MCP server that exposes
commands; the tool catalogue and registry that generate the CLI inventory; and
the shipped `aq-*` skills that teach agents the command surface.

**Key source paths.** `src/plugins/`, `src/mcp_registration.py`,
`src/mcp_interfaces.py`, `src/embedded_mcp.py`, `src/aq_uri.py`,
`src/profiles/mcp_*.py`, `src/tools/` (command catalogue, definitions,
registry, tool index), `src/known_tools.py`, `src/skills/` (aq-cli, aq-comms,
aq-knowledge, aq-mini-projects, aq-playbooks-and-gates, aq-reviews, aq-tasks,
aq-workspaces-and-git).

**Existing docs.** `docs/guides/plugins-and-mcp.md`,
`docs/reference/modules/plugins.md`, `docs/specs/plugin-system.md`,
`docs/specs/mcp-server.md`, `docs/specs/tiered-tools.md`,
`docs/specs/design/memory-plugin.md`,
`docs/superpowers/specs/2026-04-25-aq-memory-extraction-design.md`,
`docs/superpowers/specs/2026-04-25-platforms-implementation-design.md`.

**Open epics.** None.

## Merged candidates

These were considered as systems and folded in, so a later author does not
re-split them without a reason:

| Candidate | Folded into | Why |
|---|---|---|
| Database and migrations | `daemon-core` | Shared storage, no behaviour of its own; every spec names its tables. |
| Claims and the frontier | `scheduling-and-claims` | One mechanism: the frontier query is what a claim takes. |
| Waits, hibernation, handoffs, stall watch | `sessions-and-harnesses` | All describe one session's lifecycle between turns. |
| GitHub access, git identity, retained objects | `projects-workspaces-git` | One `GitManager`, one `gh` path. |
| Promotion flows and releases | `integration-and-delivery` | A promotion is a train visit; a release tags a promotion commit. |
| Agent cron, object loop | `playbooks` | Both are reviewed playbooks on top of the engine. |
| Human asks, escalations, digest, reports | `messaging-and-human-asks` | Same delivery engine and recipients. |
| Policy profiles, operator decisions, factory policy | `supervisors-and-policy` | The supervisor is the only reader of all three. |
| Spec ingestion, knowledge records, memory | `reviews-and-knowledge` | Reviewed documents in, versioned knowledge out. |
| CLI | `operations-and-recovery` plus the owning system | The CLI is a surface over commands, not a system; each spec covers its own verbs. |
| Install, deploy, update, backup | `operations-and-recovery` | Operator-only lifecycle. |
| Skills, tool catalogue | `plugins-mcp-and-tools` | Both describe how an agent discovers the surface. |

## Gaps the child specs must settle

- **Daemon-core versus scheduling.** `src/orchestrator/` is split across
  systems 1, 3 and 6 by file; the two specs must agree on which spec owns
  `run_one_cycle` ordering (this inventory says `daemon-core`).
- **Supervisors have no code package.** System 10 is a profile, a template
  and two playbooks plus `src/policy_profiles/`; its spec is mostly policy and
  must say so.
- **Vault in-flight specs are not in the repository.** Every vault citation
  above is by dated file name under `projects/agent-queue/specs/`; the child
  spec should quote the review id and approval state at writing time.
- **Open-task snapshot.** The epic column is a 2026-10-20 snapshot. Child
  specs re-query before writing their PLANNED section.
