# System spec: routing and profiles

Status: describes the system as of 2026-10-20 (base `22cffd1b7`). This is system 4 of the
eighteen in the agent-queue system inventory (vault
`projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`, review
`rev-bright-rapids`, in review at revision 1 when this was written). It explains what the
system is for, how it works today, where it stops, and what is planned. It links to the
existing concept, reference and design documents rather than repeating them.

## 1. Purpose

Agent Queue runs many agents across several CLI harnesses (Claude Code, Codex, OpenCode)
and several model families. Every task needs an answer to "who should do this": which
harness, how much model, with which permissions. Answering it by hand doesn't scale, and
answering it inside the scheduler would put judgment in mechanism. This system owns that
answer. It has three parts:

- **Profiles** describe what an agent *is*: its role prompt, rules, capability grants,
  harness, lifecycle and default intelligence class.
- **Intelligence classes** describe how much model a piece of work deserves, independently
  of vendor. `standard-high` resolves to one model on Claude and another on Codex.
- **Routing** decides, for each task, which profile runs it. A bound routing playbook
  makes the decision, and the result is written onto the task row before anything may
  claim it.

The problems it solves are:

- getting a capable, *authorised* agent on every task without the filer choosing one;
- spreading load across providers by quota, availability and live headroom;
- keeping risky work away from cheap or unproven models;
- making the choice explainable and auditable after the fact.

## 2. How it works today

### 2.1 Vocabulary

The concept guide [agents and routing](../../concepts/agents-and-routing.md) defines the
terms. In brief:

- An **intelligence class** is a vault file mapping each provider to a model slice.
- A **harness** is the CLI an agent runs in, and it fixes the provider.
- An **agent profile** is `vault/agent-types/<id>/profile.md`.
- A **rung** is a profile derived from a template for one class × harness pair.
- A **route** is the profile, class and evidence written onto a task.
- A **route source** records who wrote the route.
- **Lifecycle** is `task` (one session per task), `pool` (long-lived claiming workers) or
  `named` (the supervisor).

The [glossary](../../reference/glossary.md) has the full list.

### 2.2 Intelligence classes

Classes are markdown files with YAML frontmatter and one JSON block of provider slices.
They are seeded write-if-absent from `src/prompts/default_intelligence_classes/` into
`vault/intelligence-classes/` (`src/vault.py`). `src/intelligence_classes/registry.py`
loads them into a live, vault-backed mapping. **There is no database table.**

- A file's revision is the sha256 of its bytes. `src/intelligence_classes/editing.py`
  makes revision-checked edits.
- A parse failure keeps the previous entry and is reported by the doctor check
  `intelligence_classes.parse`.
- Provider slices that still exactly equal a historical bundled value are refreshed in
  memory. A file marked `customized: true` is never touched, and the loader never
  rewrites a file.

Seven classes ship, following the
[intelligence-class simplification design](../../superpowers/specs/2026-09-12-intelligence-class-simplification-design.md):

| Class | Anthropic | OpenAI / Codex | Google |
|---|---|---|---|
| `fast-low`, `fast-high` | claude-sonnet-5 | gpt-5.6-luna | gemini-2.5-flash |
| `standard-high` | claude-opus-5 | gpt-5.6-terra | gemini-2.5-flash |
| `deep-low`, `deep-high` | claude-fable-5 | gpt-5.6-sol | none |
| `astra-low`, `astra-high` | none | gpt-6-astra | none |

`src/profiles/class_retirement.py` migrates installs off the older twelve-class ladder at
daemon start. It repoints profile `default_class` values, renames retired class files to
`<id>.md.retired` (customised ones are kept), and disables orphaned derived rungs.
Database pins were moved by Alembic revision `a0000000000f`.

At session launch, `src/sessions/spec.py` (`_resolve_class_config`) resolves the class in
this order: the agent's own override, then the task's class, then the profile's
`default_class`. It takes the provider from the harness. The Codex CLI reads its `codex`
slice first. An empty model is a warning and leaves the session with no class
configuration; nothing falls back silently. The direct LLM path (playbook LLM steps) takes
its provider from `llm.provider` instead (`src/profiles/intelligence.py`).

### 2.3 Profiles

#### Shipped profiles

Thirteen profiles ship in `src/profiles/defaults/`, and `ensure_default_profiles`
(`src/vault.py`) seeds them write-if-absent. They fall into four groups.

**Stage profiles.** These are fixed control-plane roles:

| Profile | Harness | Class | Role |
|---|---|---|---|
| `supervisor` | claude | deep-high | Plans and steers; `named` lifecycle; never edits code |
| `triage` | claude | fast-low | Reports tasks that can't be routed |
| `reviewer` | claude | standard-high | Read-only review |
| `final-reviewer` | claude | standard-high | Read-only merge review |
| `planner` | claude | none | Turns a request into a spec and task graph |
| `playbook-compiler` | claude | fast-low | Runs playbooks, including the router itself |
| `spec-ingest` | claude | deep-high | Proposes ingest batches from specs |

**`pr-merger`.** Runs on codex at deep-high and merges only on green checks. It is
excluded from routing.

**Worker templates** (`template: true`): `worker-claude`, `worker-codex` and
`worker-opencode`.

**Operator templates** (`template: true`, `capability_sync: true`): `operator-claude` and
`operator-codex`. They grant `operator_preflight` and `operator_execute`, and destructive
operator actions still need a human decision id. See the routable-operator design (§6).

#### Profile file format

The format is specified in [agent profiles](../agent-profiles.md) and
[profiles design](../design/profiles.md), and parsed by `src/profiles/parser.py`.

- **Structured sections:** `## Config` (harness, `default_class`, lifecycle, permission
  mode, limits), `## Tools`, `## MCP Servers`.
- **Prompt sections:** `## Role`, `## Rules`, `## Reflection`.
- **Capabilities:** `## Capabilities`. Validation follows the
  [profile validation design](../../superpowers/specs/2026-04-25-profile-validation-design.md).

#### Capabilities

`src/profiles/capabilities.py` defines three namespaces:

- `harness_tools`: the CLI's own allowlist, enforced best-effort by the harness.
- `aq_commands`: `CommandHandler` command names. The server-side dispatch check enforces
  them, and this is the real security boundary.
- `plugin_tools`: plugin commands and `mcp__server__tool` names.

The rules are the same for all three. Grants are deny-by-default, an empty list means
deny, and no namespace accepts a wildcard.

#### Inheritance

`src/profiles/inheritance.py` allows a single `extends:` hop, and a template that itself
extends is an error. The stub always wins:

- `## Config` merges key by key.
- Prompt sections and `## Capabilities` are taken whole. Capabilities are not merged
  namespace by namespace.

Inheritance is resolved when the profile syncs to the database, so a derived rung never
holds a copy of its template to drift from.

#### Rungs are derived, not authored

`src/profiles/catalog.py` (`shipped_profile_catalog`) produces one `<class>-<harness>`
rung for every class whose provider slice names a model on that harness. Each rung extends
`worker-<harness>`.

- For `standard-high` and `deep-high` it also produces `operator-<class>-<harness>` rungs.
  These extend `operator-<harness>` and are pools sized 0–1.
- The derivable harnesses are claude and codex (`WORKER_PROVIDERS`). Gemini is a
  documented extension point, but no template ships for it.
- OpenCode rungs are configured by the operator from `worker-opencode`; they are not
  derived.

`aq install` probes which harnesses are installed and logged in, then calls
`refresh_catalog_profiles`. That seeds the active rungs as short stub files (`id`,
`extends`, `default_class`, lifecycle) and writes `vault/profile-activation.json`. At
daemon start, `derive_rungs_from_vault` runs the same derivation without probes. Seeding
never overwrites or deletes a file. **Edit the template or the class file, never a rung.**

#### Sync, retirement, drift and reseed

- **Sync.** `src/profiles/sync.py` turns vault markdown into `agent_profiles` rows
  (`src/database/tables.py`). Profiles are global: per-project overrides were retired by
  `src/profiles/project_override_migration.py`, and the doctor check
  `profiles.project_overrides` reports any leftovers.
- **Retirement.** Deleting a shipped default leaves a tombstone in
  `vault/agent-types/.retired-defaults` (`src/profiles/retired_defaults.py`) so the next
  start doesn't re-seed it.
- **Drift.** `src/profiles/drift.py` reports read-only drift between an installed copy and
  the shipped one (`profiles.system_drift`).
- **Reseed.** `aq agent profile-reseed --profile-id <id> [--grants-only]` is the explicit
  write path. It leaves a backup and clears the tombstone.
- **Capability sync.** This is the one automatic exception to write-if-absent.
  `src/profiles/capability_sync.py` merges shipped `## Capabilities` grants that the vault
  copy lacks, additively, on every daemon start and profile reload. It writes atomically,
  keeps a `.bak-<epoch>`, and emits `profile.capabilities_synced`. The profiles it covers
  are `supervisor`, every `worker-*` template, and any profile whose frontmatter says
  `capability_sync: true` (the operator templates). Frontmatter `capability_sync: false`
  opts out.

#### Other registries in `src/profiles/`

- **MCP server registry** (`src/profiles/mcp_registry.py`). Vault files
  `[projects/<pid>/]mcp-servers/<name>.md`, where project scope shadows system scope.
  There is no table, and a builtin `agent-queue` entry can't be edited. The tool catalogue
  comes from probing each server (`mcp_probe.py`, `mcp_catalog.py`).
- **Workspace-kind registry** (`src/profiles/workspace_kind_registry.py`). Reconciles
  vault `workspace-kinds/` files with the `workspace_kinds` table. Workspace kinds
  themselves belong to `projects-workspaces-git`; only the registry code sits here.

### 2.4 Agents

`src/agents/` covers what a running worker is, as distinct from the profile it runs:

- the `agents` identity table and generated names (`src/agent_names.py`);
- per-worker harness, model and class overrides (`configuration.py`);
- liveness: a starting, running or draining session within the lease TTL (`liveness.py`);
- session roles and flock rows (`sessions.py`, `service.py`);
- subagents and task-free terminals.

`src/agents/routing.py` holds the pure compatibility checks: `task_agent_mismatch` and
`resolve_task_profile`. A claim uses them to make sure the claiming worker can serve the
task's routed profile and class. A role profile only matches a task whose route source is
`role`.

### 2.5 Routing a task

The task row *is* the route (`src/assignment_routing.py`). Routing is **mandatory**: once
a project's router is ready, only routed work can be claimed.

#### Route columns on `tasks`

| Column | Meaning |
|---|---|
| `profile_id`, `intelligence_class` | The route itself |
| `route_source` | Who wrote it (`src/routing/sources.py`) |
| `route` | JSONB evidence: candidates, reason, policy digest, live context, override |
| `class_hint` | Optional hint |
| `prefer_target`, `prefer_mode` | Optional preference, `soft` or `strict` |
| `provider_intent` | `pinned`, `preferred` or `class_only` |
| `rerouted_from` | Earlier profile when rerouted |

The named check constraints are `ck_tasks_route_source`, `ck_tasks_prefer_mode`,
`ck_tasks_provider_intent` and `ck_tasks_route_source_profile`. The last one requires
`(profile_id IS NULL) = (route_source = 'unrouted')`, so every profile write declares
where it came from.

Each project names its router in `projects.assignment_playbook_id`, which defaults to the
`routing.default_router` key, `default-assignment-routing`.

#### Route sources

| Source | Written by | Claimable |
|---|---|---|
| `unrouted` | Default; a reset | Never once the router is ready |
| `router` | The bound routing playbook via `task_route_apply` | Yes |
| `override` | An audited operator or supervisor override | Yes |
| `role` | A control-plane stage task (`ROLE_PROFILE_IDS`: triage, spec-ingest, reviewer, final-reviewer, fidelity-reviewer) | Yes; never routed |
| `legacy` | Pre-router writes or manual non-override routes | Only while the router is not ready |

Filing surfaces refuse to accept a route choice from the filer (`routing.choice_forbidden`,
`src/routing/filing.py`). The one exception is a service or playbook principal filing a
role task. The supervisor can never be a task route (`src/profiles/task_execution.py`).

#### The flow

1. **Readiness.** `src/routing/readiness.py` decides, once per orchestrator cycle, whether
   each project's bound router is ready. A router is ready when an enabled activation of
   that playbook grants `task_route_apply`. Before that, legacy routes stay claimable.
2. **Route needed.** `src/orchestrator/route_needed.py` is the scheduler's only
   involvement. It emits `task.route_needed` for each unrouted, unassigned, non-container
   task in an active project, or for each legacy task once the router is ready. The task
   must be READY or BLOCKED, or DEFINED and held only by its own routing gate.
   - Each task is throttled to one emit every 120 s, recorded only after a successful
     emit.
   - Containers are never routed.
   - Where this runs inside the cycle is daemon-core's concern (`run_one_cycle` ordering
     belongs to `daemon-core`).
3. **Plan.** The shipped router
   (`src/prompts/default_playbooks/default-assignment-routing.md`) runs as
   `playbook-compiler` at `fast-low`. It calls `task_route_plan` (read-only, contract READ
   in `src/commands/contracts/builtin.py`), which runs the pure planner
   `src/routing/planner.py` against the task's facts and a snapshot. The snapshot holds
   profiles, provider availability, busy and backlog counts, quota windows and bounded
   live headroom (`src/routing/context.py`). The planner's stages are:
   1. **Kind and class.** Resolve the kind and origin rule (keyed by `created_by_kind`).
      The class comes from the hint, then the classification, then the kind default. It is
      clamped to `max_class` and raised to the risk floor. Bugfix escalation applies here.
   2. **Candidates.** Build candidate cells:
      - Laned kinds use their lane.
      - General work excludes narrow harnesses and the OpenCode family.
      - The filters are reserved cells, excluded providers, workspace requirements, the
        local-model gate and risk harnesses.
   3. **Preference.** Apply it. A strict preference with nothing serving it refuses with
      `prefer_target_unavailable` and records why each target profile was excluded; a soft
      one only marks candidates.
   4. **Availability.** No available candidate means the task is `held`.
   5. **Classification.** If the answer depends on facts nobody has given, the outcome is
      `needs_classification`, with the questions and allowed answers.
   6. **Score and choose.** Score
      `pressure = (busy + backlog + 1) / (slots × weight × usage × headroom × availability)`
      and choose. Free reserved, preferred and hosted cells come first, and ties go to the
      lane's preference order. Priority is never an input.
   7. **Reason.** Write a reason of at most 600 characters.
4. **Classify.** On `needs_classification`, the playbook asks a `playbook-compiler` LLM
   step the planner's questions (kind, class, risk, narrowness, test verification) and
   plans again. A missing risk answer is treated as `medium` and recorded with
   `assumed: true`. The third plan has `failed: true`.
5. **Apply.** `task_route_apply` is callable only by the bound router's playbook
   invocation holding the grant; anyone else gets `routing.not_router`.
   - It takes the fleet routing lock, then re-reads the task under a row lock, because a
     route reset doesn't take the fleet lock.
   - It re-validates the chosen candidate and re-runs availability, scoring and choice
     (`reselect`) on fresh state.
   - It writes the route with `route_source = router`, or answers `stale`. A lock conflict
     is `routing.busy`.
6. **Claim.** Claims and the frontier (`scheduling-and-claims`) accept only claimable
   route sources and use the `src/agents/routing.py` compatibility check to match a worker
   to the routed profile and class.

#### Re-routing and overrides

- **Re-route.** `aq task route` (`task_route`) can be called by the local operator, a live
  supervisor, or a worker for a task it filed. It stores new hints or a preference, resets
  the task to `unrouted`, clears the throttle and emits immediately. It refuses role tasks
  and claimed or running tasks. See [task reroute recovery](../task-reroute-recovery.md)
  and [stopped-task rerouting](../../superpowers/specs/2026-10-12-stopped-task-rerouting.md).
- **Override.** `aq task route-override` (`task_route_override`) is for the operator or a
  live supervisor only. It needs a reason of 10–400 characters and refuses role, control
  and non-candidate profiles. It writes `route_source = override`, `provider_intent =
  pinned`, and an audit record in `route.override`.
- **Preference.** Operators can set a soft or strict preference on routine work as
  described in [routine routing preference](../../guides/routine-routing-preference.md)
  and [strict-preference refusals](../../superpowers/specs/2026-10-20-strict-preference-refusals.md).

#### Escalation after failure

A kind rule can say `escalate: {after_failures: N, to_class: ...}`. The shipped policy
uses this only for `bugfix`, which goes to `deep-high` after one failure. Until the
threshold the kind's default class is forced. After it, the planner routes at `to_class`
and ignores narrowness, hints and the risk floor. The route records
`escalated_after_failure`. Rework of a completed task (`reopen_with_feedback`) and
infrastructure failures don't count (see
[routing live context](../design/routing-live-context.md)).

#### Explaining and replaying

- `src/routing/explain.py` feeds `aq task explain` with one of `router_unbound`,
  `router_not_ready`, `route_failed`, `route_no_candidates`, `route_held` or
  `awaiting_route`.
- `src/routing/replay.py` (`python -m src.routing.replay`) re-plans recorded router routes
  offline against a candidate policy. It is counterfactual only and has no queue
  simulation.
- The [benchmark routing export](../../guides/benchmark-routing-export.md) covers
  measurement arms.

### 2.6 The shipped policy

The policy is a YAML block inside the playbook, validated by `src/routing/policy.py` and
identified by `policy_sha256`. It names profiles by `(class, harness)` and never by rung
id. Harness selectors are an exact id or a `prefix*`. As shipped:

- **Kinds.**
  - `feature`, `refactor`, `test` and `docs`: standard-high, narrow-eligible, preferring
    codex.
  - `bugfix`: standard-high, escalating.
  - `design` and `plan`: deep-high in the `code-design` lane, Claude preferred with Codex
    as fallback.
  - `art`: the `art-design` lane, Codex only, held.
  - `research`: standard-high.
  - `chore` and `sync`: fast-high.
  - `operator`: standard-high, preferring codex.
- **Origins.** Integration and development repairs are never narrow. Review dispatch goes
  to the `design-review` lane.
- **Risk floors.**
  - `medium`: at least standard-high on claude, codex or opencode.
  - `high`: standard-high on claude or codex.
  - `very_high`: deep-low on claude or codex, relaxed to standard-high when the task is
    narrow and test-verified.
- **Narrow lanes** send small, verifiable work to OpenCode: local models for low risk,
  `narrow-medium` for medium risk at standard-high, and `opencode-zen*` hosted models for
  low risk. Claude `deep-high` is reserved for the design and design-review lanes.
- **Balance.** All harness weights are 1.0. The usage soft limit is 80% (floor factor 0.1)
  and the degraded factor is 0.5. Ties go to codex first. The `usage_mode: headroom`
  option exists but the shipped policy doesn't set it.

The playbook's prose sections ("Earning a wider lane", "Classifying a task", "Failure
handling") are the policy's rationale. The decisions behind the risk floors are recorded in
`docs/superpowers/specs/2026-10-09-risk-aware-routing/` (README, dry run and bundle). The
activation handoff's Decision A is "landing equals activation". Its open questions B and D
were decided in `09c2ada6d`, and priority was removed from routing in `aa5390e18`.

The reviewed bundle lives in `src/prompts/reviewed_playbooks/default-assignment-routing/`.
Changing the policy means editing the playbook, rebuilding the bundle and updating its
manifest digests (see the repository's `AGENTS.md`).

### 2.7 Doctor checks

| Check | Reports |
|---|---|
| `routing.bypassed` | Queued unrouted work in a ready project, bad bindings (fixable), routers not ready, tasks unrouted for over 15 minutes, open overrides, in-flight legacy routes, lost pins |
| `routing.unmatched_selectors` | Lane selectors that match no installed harness, and OpenCode-family harnesses no narrow lane covers |
| `profiles.system_drift` | Installed shipped profiles that differ from the shipped copy |
| `profiles.project_overrides` | Leftover per-project profile overrides |
| `profiles.supervisor_capability_drift` | Grants the synced profiles lack |
| `intelligence_classes.parse` | Class files that fail to parse |

## 3. Boundaries

| Neighbour | This system owns | The neighbour owns |
|---|---|---|
| `daemon-core` | The `route_needed` emitter and readiness refresh as callables | When they run in `run_one_cycle` |
| `task-graph` | Route columns and their constraints; which tasks need a route | Task hierarchy, containers, gates, statuses |
| `scheduling-and-claims` | The route a claim must honour and the worker–task compatibility check | The frontier, claims, pools and sizing (`aq pool`) |
| `sessions-and-harnesses` | Which harness and class a profile names; class resolution rules | Harness templates (`src/sessions/default_harnesses/`), launching tmux sessions, applying the slice |
| `playbooks` | The routing policy, the router playbook's content and its reviewed bundle | The V2 runtime, activations, reviewed-bundle mechanics, `playbook-compiler` execution |
| `providers` | The candidate order recorded in `tasks.route`, which capacity spill must respect | Availability, quota windows, failover reroute (`src/providers/reroute.py`, driven by the `provider-failover` playbook through `provider_reroute`) and capacity spill (`src/providers/spill.py`) — see [provider failover](../provider-failover.md) |
| `supervisors-and-policy` | That the supervisor is never a task route; operator rungs and their grants | Supervisor behaviour, operator authority, human decision ids |
| `configuration-and-vault` | Profile, class, MCP-server and workspace-kind file formats and their sync | Vault layout, seeding mechanics, `config.yaml`, secrets |
| `projects-workspaces-git` | Respecting a task's workspace requirements when choosing candidates | Workspace kinds, workspace preparation, `task_workspace_requirements` |
| `reviews-and-knowledge` | Routing review-dispatched tasks to the `design-review` lane | Review dispatch; reviewers are filed unrouted with a class hint (`doc_review_dispatches`) |
| `plugins-mcp-and-tools` | The `plugin_tools` grant namespace and the MCP registry projection | Plugins, MCP servers themselves, tool execution |
| `api-and-clients`, `dashboard` | Command results for routing and profiles | Their presentation |
| `operations-and-recovery` | The routing and profile doctor checks listed above | The doctor framework and recovery procedures |

## 4. Principles and invariants

- **The router decides; the scheduler doesn't.** Scheduling only emits `task.route_needed`
  and enforces claimability. The choice is a playbook driving commands (mechanism in code,
  policy in playbooks). The planner is pure and deterministic; the only LLM call is the
  classification step.
- **Routing is mandatory.** Once a project's router is ready, nothing claims unrouted or
  legacy work. Every profile write declares its route source (`ck_tasks_route_source_profile`).
- **Filers don't choose routes.** Only the bound router writes `router` routes, and only
  audited overrides bypass it. The supervisor is never a task route.
- **The task row is the route.** No separate assignment table is read at claim time, and
  the route's JSONB evidence is enough to explain and replay the choice.
- **Apply re-checks.** A plan is advisory. Apply re-reads the task under lock and re-runs
  availability and choice on fresh state, or answers `stale`.
- **Priority is not a routing input.** Priority orders the frontier; it doesn't buy a
  bigger model.
- **Risk raises the floor.** A missing risk answer is treated as medium, not low.
- **Profiles are global, and rungs are derived.** Edit the template or class file, never a
  rung, and never hard-code a rung id in help text or policy.
- **Shipped content is write-if-absent.** The only automatic write to an installed profile
  is the additive capability sync.
- **Capabilities deny by default.** `aq_commands` is enforced server-side, and wildcards
  are refused everywhere.
- **Classes are vendor-neutral.** A class names an effort level. The harness picks the
  provider, and a missing slice means the harness can't serve that class.

## 5. Known gaps

These were found while writing this spec. None is tracked by an open task as of
2026-10-20 unless noted.

1. **Stale capability-sync documentation.**
   - [agents and routing](../../concepts/agents-and-routing.md) still says the
     supervisor's `## Capabilities` is the one sync exception.
   - Comments in `src/orchestrator/core.py` and `src/profiles/sync.py` say the same.
   - The doctor check is named `profiles.supervisor_capability_drift`.

   In fact the `worker-*` templates and any `capability_sync: true` profile (the operator
   templates) sync too.
2. **Stale module reference.** [the routing module reference](../../reference/modules/routing.md)
   is out of date:
   - It omits `catalog.py`, `inheritance.py`, `class_retirement.py`, `task_execution.py`,
     the `mcp_*` modules, the workspace-kind modules, `src/routing/context.py`,
     `src/routing/replay.py`, `src/routing/operator_authority.py` and
     `src/agents/sessions.py`.
   - It counts two worker templates (there are three, plus two operator templates).
   - It lists only `routing.bypassed` among the routing doctor checks.
   - It describes the generic worker ladder as matched by id prefix. Today it is matched
     by lifecycle, harness and `default_class`, and ids are explicitly not a contract.
3. **The concept guide predates risk-aware routing.** It doesn't cover:
   - risk floors, origins, `narrow-medium` and the OpenCode-family exclusion;
   - bugfix escalation;
   - the strict-preference refusal;
   - the apply row lock, `stale` and `routing.busy`;
   - the score formula and headroom;
   - `routing.unmatched_selectors`, replay and live context.

   It also lists gemini as a harness, though no gemini template or rung ships, and it
   omits OpenCode.
4. **`class_order` doesn't match the shipped classes.** The shipped policy's `class_order`
   names `fast-off` and `standard-low`, which no longer ship, and omits `astra-low` and
   `astra-high`. Astra classes are therefore unreachable through the router.
5. **The lane-earning prose contradicts the lanes.** "Earning a wider lane" says there is
   no stage 3, so medium-risk work never goes to a free hosted or local model. Yet
   `narrow-medium` sends medium-risk narrow work to local OpenCode at standard-high. One
   of them needs to change.
6. **No track record is measured.** Promoting a hosted model between lane stages (ten
   clean passes in a 14-day window) is a manual reviewed policy change. The playbook says
   so ("Nothing in the router measures a track record").
7. **A vestigial table.** `task_assignment_routes` (`src/database/tables.py`) and
   `src/database/queries/assignment_route_queries.py` have no production caller, since
   the task row became the route.
8. **Unused profile fields.**
   - A profile's `## MCP Servers` reaches only a launch log line
     (`src/orchestrator/execution.py`); the registry's adapter output is used only by
     `mcp_commands`.
   - The profile `workspaces` field is validated but not persisted. Routing reads the
     task's workspace requirements instead.
9. **The `fidelity-reviewer` role is ahead of its profile.** `fidelity-reviewer` is in
   `ROLE_PROFILE_IDS`, the review and control sets and `src/agents/routing.py`, but no
   shipped profile exists in this base. Its profile (nimble-lantern-37.1, completed)
   is still on its delivery branch.
10. **Stale code text.**
    - The `src/profiles/sync.py` docstring still describes the retired
      `projects/*/agent-types` override pattern.
    - `src/vault.py` keeps a "three defaults" docstring and a twelve-class ladder comment.
    - `src/vault.py` keeps the legacy `ensure_claude_*` profile helpers.

## 6. Planned work

No routing-owned epic is open. These open items change this system or depend on it. Task
states were re-queried on 2026-10-20 just before writing.

| Item | State | What it changes |
|---|---|---|
| clear-impact-72.2 — evaluate rewriting `default-assignment-routing` onto playbook building blocks (parent clear-impact-72, in progress) | READY | Evaluation only. If worthwhile, it writes vault plan `plans/2026-10-20-playbook-rewrite-default-assignment-routing.md` and files one implementation task. The router's behaviour and policy stay as they are. It builds on the building-blocks spec `2026-10-12-implementation-playbooks-reusable-building-blocks.md` and waits on the building blocks landing. |
| wise-orbit-24 — playbooks refactor F3: acceptance, migration and rollback rehearsal, activation handoff | BLOCKED | Activation handoff for refactored shipped playbooks, which would include the router's bundle. Owned by `playbooks`. |
| nimble-lantern-37 — held ingestion P5: policy, profiles, pipeline, docs (spec `2026-10-14-spec-ingestion-held-batches-…`, review `rev-agile-beacon`, approved at revision 1) | IN_PROGRESS | Adds the `fidelity-reviewer` role profile (37.1, completed, awaiting delivery). 37.2 (READY) narrows the shipped `spec-ingest` grant: it drops `task_batch_commit`, which now answers `fidelity_review_required`. Installed copies keep the old grant until reseeded, because `spec-ingest` isn't capability-synced. 37.3 (DEFINED) covers docs. |
| stark-willow-95.17 — align shipped policy, profile instructions and retirement ratchets (spec `2026-10-12-fast-smooth-integration.md`, revision 2) | DEFINED | Updates worker templates, prime text and skills. Capability sync may add only the scoped parent-candidate and fixer grants, and installed drift is reported rather than overwritten. |
| stark-willow-95.13 — one admission view for frontier, pools explain and delivery guards | READY | A boundary item. Claim admission moves to a single view; route claimability is one of its inputs. Owned by `scheduling-and-claims`. |
| eager-vault-55.19 — rewrite the vision spec from all eighteen system specs | DEFINED | Consumes this section. |

Vault specs behind the system as built, with their review state at writing time:

| Spec (vault `projects/agent-queue/specs/`) | Review | State |
|---|---|---|
| `2026-09-28-mandatory-task-routing.md` (keen-vault-26) | `rev-vivid-impact` | approved |
| `2026-10-09-routing-policy-revision-risk-aware-routing-default-assignmen.md` (smart-beacon-29) | `rev-azure-flare` | approved |
| `2026-10-09-activation-handoff-risk-aware-default-assignment-routing-art.md` (solid-apex-59) | `rev-wise-impact` | approved |
| `2026-10-10-routable-operator-workers-scoped-setup-authority-and-human-g.md` (grand-nexus-50) | `rev-vivid-dune` | approved |
| `2026-10-14-spec-ingestion-held-batches-…` (nimble-lantern-37) | `rev-agile-beacon` | approved, revision 1 |

The vault plan `plans/2026-09-27-provider-worker-allocation-controls.md` records the
earlier provider allocation controls that the balance settings came from.

The gaps in §5 are candidates for follow-up work. None has a filed task.

## 7. Related documents and source

**Concepts and guides**
- [Agents and routing](../../concepts/agents-and-routing.md)
- [Worker pools](../../guides/worker-pools.md)
- [Routine routing preference](../../guides/routine-routing-preference.md)
- [Benchmark routing export](../../guides/benchmark-routing-export.md)

**Reference**
- [Routing module reference](../../reference/modules/routing.md)
- [Profiles and classes](../../reference/profiles-and-classes.md)
- [Glossary](../../reference/glossary.md)

**Specs and designs**
- [Agent profiles](../agent-profiles.md)
- [Profiles design](../design/profiles.md)
- [Routing live context](../design/routing-live-context.md)
- [Task reroute recovery](../task-reroute-recovery.md)
- [Provider failover](../provider-failover.md)

**History**
- [Assignment routing as a playbook](../../superpowers/specs/2026-09-06-assignment-routing-as-playbook.md) (historical)
- [Playbook intelligence routing](../../superpowers/specs/2026-08-31-playbook-intelligence-routing-design.md)
- [Profile validation](../../superpowers/specs/2026-04-25-profile-validation-design.md)
- [Intelligence-class simplification](../../superpowers/specs/2026-09-12-intelligence-class-simplification-design.md)
- [Supervisor task-routing guard](../../superpowers/specs/2026-09-10-supervisor-task-routing-guard.md)
- [Agent question routing](../../superpowers/specs/2026-08-30-agent-question-routing.md)
- [Risk-aware routing](../../superpowers/specs/2026-10-09-risk-aware-routing/README.md)
- [Stopped-task rerouting](../../superpowers/specs/2026-10-12-stopped-task-rerouting.md)
- [Strict-preference refusals](../../superpowers/specs/2026-10-20-strict-preference-refusals.md)

**Source**
- Routing: `src/routing/` (`policy.py`, `planner.py`, `readiness.py`, `sources.py`,
  `filing.py`, `explain.py`, `context.py`, `replay.py`, `operator_authority.py`),
  `src/assignment_routing.py`, `src/commands/routing_commands.py`,
  `src/orchestrator/route_needed.py`, `src/doctor/routing_checks.py`
- Profiles: `src/profiles/` (`parser.py`, `capabilities.py`, `inheritance.py`,
  `catalog.py`, `sync.py`, `capability_sync.py`, `drift.py`, `retired_defaults.py`,
  `class_retirement.py`, `task_execution.py`, `default_selection.py`, `mcp_registry.py`,
  `workspace_kind_registry.py`), `src/profiles/defaults/`
- Classes: `src/intelligence_classes/`, `src/prompts/default_intelligence_classes/`
- Agents: `src/agents/`, `src/agent_names.py`
- Policy: `src/prompts/default_playbooks/default-assignment-routing.md`,
  `src/prompts/reviewed_playbooks/default-assignment-routing/`
- Tables: `tasks` route columns, `agent_profiles`, `task_reroutes`, `agents`,
  `workspace_kinds`, `task_assignment_routes` (vestigial) in `src/database/tables.py`
