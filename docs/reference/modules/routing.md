# Module catalog — routing

Agents, profiles, intelligence classes and mandatory task routing: the 32
production modules that decide *which* worker runs a task and *how hard it
thinks*, plus the shipped markdown they read. Two modules owned by other shards
are listed here too because routing lives in them: the routing commands (CLI
shard) and the routing playbook (playbooks shard).

Prose for everything here lives on two pages:

* [Agents and routing](../../concepts/agents-and-routing.md) — the concepts,
  the routing flow, compatibility, failures and recovery.
* [Profile and class reference](../profiles-and-classes.md) — every field, the
  override precedence, worked examples and the operator commands.

Related shards: [sessions](sessions.md) owns launching the CLI a profile
selects; the [scheduler shard](scheduler.md) owns pool sizing, the agent
reconciler and `task.route_needed` emission
([`src/orchestrator/route_needed.py`](../../../src/orchestrator/route_needed.py));
the [CLI shard](cli.md) owns the `aq agent` / `aq task route` command surface.

## The router

Every worker route is written by the project's bound routing playbook. Filing
carries hints (an intelligence class and a kind), never a profile; the router
plans a route from them and writes it; once the router is ready, the claim
frontier and the push scheduler accept only routed work.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/routing/__init__.py`](../../../src/routing/__init__.py) | Package docstring only — mandatory task routing. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Kept empty on purpose: `src.models` imports `src.routing.sources`, so anything imported here would load with every model. |
| [`src/routing/sources.py`](../../../src/routing/sources.py) | The `tasks.route_source` values (`unrouted`, `router`, `override`, `role`, `legacy`), the sources a claim accepts before and after the router is ready, the role profile ids, `DEFAULT_ROUTER_PLAYBOOK_ID`, and `declared_route_source`, which refuses a profile write that names no source. | [reference/database/tables.md](../database/tables.md) | Dependency-free because `src.models` and the migrations import it. Mirrors `ck_tasks_route_source` and `ck_tasks_route_source_profile`. `tests/test_task_route_source.py` |
| [`src/routing/filing.py`](../../../src/routing/filing.py) | Filing carries hints, never routes: `routing_choice_refusal` answers `success: false`, `code: routing.choice_forbidden` for a profile, provider, model, harness, agent type, pin, provider intent, preferred provider or default profile on every filing surface and on `create_project` / `edit_project`, and names what to pass instead. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Runs in `CommandHandler.execute` ahead of args-model validation, so every principal gets the same answer. The one exception is a role profile (`triage`, `spec-ingest`, `reviewer`, `final-reviewer`) named by a `SERVICE` or `PLAYBOOK` principal to `create_task` / `ensure_task`. Graph documents are refused where they are parsed (rule `routing_choice_forbidden`). `tests/test_routing_mandatory.py` |
| [`src/routing/policy.py`](../../../src/routing/policy.py) | Parses and validates the routing policy — the fenced YAML block in the routing playbook's `## Routing policy` section — into a `RoutingPolicy` (kinds, origins, lanes, reserved cells, balance weights) and its `policy_sha256` digest. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Profiles are named by `(class, harness)`, never by rung id, so a policy survives an install that derives different rungs. `tests/test_routing_planner.py` |
| [`src/routing/planner.py`](../../../src/routing/planner.py) | Deterministic route selection: kind and class (hint beats classification beats the kind default, clamped at `max_class`), candidates (lane, reserved cells, excluded and non-preferred providers, workspace needs), availability, whether an LLM classification is needed, the load score, the choice and the one-sentence reason. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Pure: no database, clock or provider reads. `task_route_plan` builds one capacity `Snapshot` and calls `plan_route`; `task_route_apply` re-runs availability, score and choice on a fresh snapshot through `reselect`. `tests/test_routing_planner.py` |
| [`src/routing/readiness.py`](../../../src/routing/readiness.py) | Router readiness and binding state: a project's router is ready when its bound playbook has an enabled system or project activation whose artifact grants `task_route_apply`; a binding is otherwise `unbound`, `missing`, `not_router` or `not_ready`. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Refreshed once per orchestrator cycle and shared by emission, the push scheduler, pool demand and pool claims. Before readiness `legacy` work stays claimable, so a failed router activation cannot stop the factory. `tests/test_routing_enforcement.py` |
| [`src/routing/explain.py`](../../../src/routing/explain.py) | Why the router has not routed a task, for `aq task explain`: `router_unbound`, `router_not_ready`, `route_failed`, `route_no_candidates`, `route_held` or `awaiting_route`. | [reference/cli/README.md](../cli/README.md) | Pure. Reads the `plan_a` / `plan_b` / `plan_c` bindings of the newest router run for the task. `tests/test_explain.py`, `tests/test_task_route.py` |
| [`src/commands/routing_commands.py`](../../../src/commands/routing_commands.py) | The router's commands and the two human levers: `task_route_plan` (READ: applies the policy to the task's hints and a fleet snapshot), `task_route_apply` (UPDATE: only the bound router may call it; writes `profile_id`, `intelligence_class`, `provider_intent`, `route_source='router'` and the `route` record, resolves open `routing` gates, emits `task.routed`), `task_route` (re-runs the router on an unclaimed task, optionally with new hints) and `task_route_override` (the audited emergency override). | [playbook-commands/task_route_plan.md](../playbook-commands/task_route_plan.md) | Owned by the CLI shard; listed here because it is where a route is written. The load reads and guarded writes are in [`src/database/queries/routing_queries.py`](../../../src/database/queries/routing_queries.py). `tests/test_routing_router.py`, `tests/test_task_route.py`, `tests/test_task_routing_contract.py` |
| [`src/doctor/routing_checks.py`](../../../src/doctor/routing_checks.py) | `aq doctor --check routing.bypassed`: queued work in a ready project whose route is not `router`, `override` or `role`; unbound, missing or non-routing bindings (`--fix` binds them to `routing.default_router`); routers that are not ready; unrouted tasks older than 15 minutes; open overrides; in-flight legacy routes. | [guides/operations.md](../../guides/operations.md) | `--fix` edits project rows only, never tasks. `tests/test_routing_doctor.py` |

## The route

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/assignment_routing.py`](../../../src/assignment_routing.py) | States that the task row *is* the route: reads `tasks.intelligence_class`, which the router's `task_route_apply` writes with the profile, decides nothing, and keeps the stub seam tests inject through. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Exists to document an absence — there is no separate routing-decision table. A task without a claimable route has none here, and the cascade emits `task.route_needed` until the router gives it one. `tests/test_assignment_routing.py` |
| [`src/agents/routing.py`](../../../src/agents/routing.py) | The compatibility rules as one pure function: `task_agent_mismatch` returns `None` or the sentence saying why this worker cannot serve this task. Also `resolve_task_profile`, which resolves the task's own `profile_id` and nothing else. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | No I/O, no mutation; availability and capacity are the caller's problem. There is no project default to fall back to: an unrouted task resolves to no profile. The generic worker ladder is matched by id prefix plus `harness == "claude"`, never an enumerated set. `tests/test_agent_task_routing.py` |

## Worker identity

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/agents/__init__.py`](../../../src/agents/__init__.py) | Package docstring only — global worker identity, configuration and observability. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | No symbols; import from the submodules. |
| [`src/agents/configuration.py`](../../../src/agents/configuration.py) | Applies one worker's saved `harness` / `model` / `intelligence_class` onto a copy of its profile, resolves the launch settings that copy implies, and registers the singleton supervisor row. | [reference/profiles-and-classes.md](../profiles-and-classes.md) | Changing harness deliberately drops an inherited model belonging to the old CLI. `tests/test_agent_flock.py`, `tests/test_codex_tier_classes.py` |
| [`src/agents/service.py`](../../../src/agents/service.py) | Builds the flock view: every durable worker joined to its current session, task, question, sub-agent counts and liveness. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | A live session reports its own frozen launch settings, never today's edited profile. `tests/test_agent_flock.py`, `tests/test_agent_flock_lifecycle.py` |
| [`src/agents/liveness.py`](../../../src/agents/liveness.py) | The single definition of "is this agent alive": a live session whose `last_activity` is inside the lease TTL. | [concepts/sessions.md](../../concepts/sessions.md) | `agents.last_heartbeat` is task-scoped and is **not** liveness — an idle pool worker reads hours stale by design. `tests/test_agent_liveness.py` |
| [`src/agents/subagents.py`](../../../src/agents/subagents.py) | Counts a worker's children in two populations — AQ tasks its session created, and children the harness itself spawned — and adds them. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | A session launched without its hook file yields `None`, not a confident zero. `tests/test_agent_subagents.py` |
| [`src/agents/terminals.py`](../../../src/agents/terminals.py) | Starts, resumes and stops a task-free interactive terminal for one worker (`aq agent start-terminal`). | [reference/terminals-and-claims.md](../terminals-and-claims.md) | Requires a provider with `Cap.INPUT`; the session name is a hash of the agent id so it cannot traverse paths. `tests/test_agent_terminals.py` |
| [`src/agent_names.py`](../../../src/agent_names.py) | Generates memorable agent display names from four weighted word pools, with a collision-retry helper. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | **No production caller on `main`** — the reconciler names workers `<profile-id>-<n>`. Kept as a library and covered by `tests/test_agent_names.py`. |

## Intelligence classes

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/intelligence_classes/__init__.py`](../../../src/intelligence_classes/__init__.py) | Parses a class file (frontmatter plus one JSON block), resolves `(class, provider) → slice`, and refreshes provider slices that are still byte-identical to a historical bundled default. | [reference/profiles-and-classes.md](../profiles-and-classes.md) | The refresh is per provider and never rewrites the file; `customized: true` opts out. `tests/test_intelligence_classes.py`, `tests/test_codex_fast_classes.py` |
| [`src/intelligence_classes/registry.py`](../../../src/intelligence_classes/registry.py) | A live `Mapping` of `{class_id: IntelligenceClass}` kept current by the shared vault watcher, plus the parse-error record `aq doctor` reads. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | A parse failure keeps the previous entry — a half-saved file must not take a class offline mid-run. `tests/test_intelligence_class_registry.py` |
| [`src/intelligence_classes/editing.py`](../../../src/intelligence_classes/editing.py) | Validated, revision-checked writes to an existing class file: per-provider effort vocabularies, atomic replace under a lock, stale-write conflict. | [reference/profiles-and-classes.md](../profiles-and-classes.md) | Backs `aq system edit-intelligence-class`. `tests/test_intelligence_class_editing.py` |

## Profiles

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/profiles/__init__.py`](../../../src/profiles/__init__.py) | Package docstring and submodule map. | [reference/profiles-and-classes.md](../profiles-and-classes.md) | All public symbols live on the submodules. |
| [`src/profiles/parser.py`](../../../src/profiles/parser.py) | The profile markdown format: frontmatter, the structured JSON sections, the prompt sections, and every `## Config` key's validation. | [reference/profiles-and-classes.md](../profiles-and-classes.md) | Deterministic — invalid JSON is a parse error, never a silent fallback. Rejects the retired `runtime`, `agent_name` and `model` keys with a pointer to `harness`. `tests/test_profile_parser.py`, `tests/test_profile_parser_runtime.py`, `tests/test_profile_session_fields.py` |
| [`src/profiles/sync.py`](../../../src/profiles/sync.py) | Upserts a parsed profile into `agent_profiles`, and is the vault watcher's handler for `agent-types/*/profile.md`. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Vault mechanics are `docs/concepts/configuration-and-vault.md` (planned). A failed sync leaves the previous row active. Tool names are soft-validated; MCP entries are names, resolved later. `tests/test_profile_sync.py`, `tests/test_agent_profiles.py` |
| [`src/profiles/capabilities.py`](../../../src/profiles/capabilities.py) | The immutable three-namespace capability policy, its canonical serialization, and the legacy `allowed_tools` adapter. | [reference/profiles-and-classes.md](../profiles-and-classes.md) | No namespace accepts a wildcard; empty means *none*, `None` on the profile means *not authored*. `tests/test_capability_policy.py`, `tests/test_capability_operator_surfaces.py` |
| [`src/profiles/intelligence.py`](../../../src/profiles/intelligence.py) | Read-only answer to "what provider and model does this profile resolve to", separately for session launch (harness fixes the provider) and the direct LLM path (`llm.provider` does). | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Pure, snapshot-injected, so a projection can state policy without building a session spec. `tests/test_profile_intelligence.py` |
| [`src/profiles/default_selection.py`](../../../src/profiles/default_selection.py) | The profile-id sets for the stage profiles that are never an ordinary worker route: `SPECIAL_PURPOSE_PROFILE_IDS` and `EXCLUDED_PROFILE_IDS` (the supervisor). | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Constants only. It no longer picks a project's fallback profile: projects have no default profile and are bound to a router instead. `catalog._stage_profile_ids` builds the stage-profile set from these. |
| [`src/profiles/drift.py`](../../../src/profiles/drift.py) | Compares each vault copy of a shipped profile against the in-tree default on the semantic `## Config` fields, missing/extra sections and `## Capabilities` grants, and performs either repair — a full reseed or an additive grants-only merge — with a backup. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | `## Config` compares `read_only`, `harness`, `lifecycle`, `needs_workspace` only; grants compare per namespace (`harness_tools`/`aq_commands`/`plugin_tools`), extra vault grants are never drift; everything else is operator tuning. Backs `aq agent profile-drift` / `profile-reseed [--grants-only]` and `doctor --check profiles.system_drift`. `tests/test_profile_drift.py` |
| [`src/profiles/capability_sync.py`](../../../src/profiles/capability_sync.py) | Merges the shipped `## Capabilities` grants a synced profile's vault copy lacks into it — the supervisor by default, on daemon start and on every vault reload of that file — and emits `profile.capabilities_synced` naming each grant added. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Additive only, through `drift.merge_profile_grants` (atomic write, `.bak-<epoch>`). Frontmatter `capability_sync: false` opts a profile out, `true` opts another shipped profile in. Backs `doctor --check profiles.supervisor_capability_drift` and its `--fix`. `tests/test_supervisor_capability_sync.py` |
| [`src/profiles/retired_defaults.py`](../../../src/profiles/retired_defaults.py) | The tombstone file recording which shipped profile ids an operator deliberately deleted, so write-if-absent seeding does not resurrect them. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Leaf module, stdlib only, because `src.vault` and the profile commands both need it. A corrupt file reads as "nothing retired". `tests/test_retired_defaults.py` |

## One-shot migrations

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/profiles/migration.py`](../../../src/profiles/migration.py) | Generates vault markdown from pre-vault `agent_profiles` rows, verifying each file round-trips back to the original. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Idempotent: a profile that already has a vault file is skipped. `tests/test_profile_migration.py`, `tests/test_startup_profile_migration.py` |
| [`src/profiles/model_pin_migration.py`](../../../src/profiles/model_pin_migration.py) | Strips the retired `Config.model` pin from every vault profile, before parsing, since the parser now rejects the key. | [reference/profiles-and-classes.md](../profiles-and-classes.md) | Always removes the pin; a pin that disagreed with the class's model is logged so an operator can audit it. `tests/test_profile_model_pin_migration.py` |
| [`src/profiles/project_override_migration.py`](../../../src/profiles/project_override_migration.py) | Promotes any surviving `project:<pid>:<id>` profile into its system profile and deletes the override. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | `## Config` keys merge last-writer-wins; differing prose is reported, never concatenated. Backs `doctor --check profiles.project_overrides --fix`. `tests/test_project_override_migration.py` |

## Shipped markdown

These are content, not code: they ship in the repository, are copied into a
vault only if absent, and are edited by operators from then on. They are
covered as families rather than file by file.

| Family | Files | Purpose | Component |
|---|---|---|---|
| [`src/prompts/default_intelligence_classes/`](../../../src/prompts/default_intelligence_classes/) | 7 — `fast-{low,high}`, `standard-high`, `deep-{low,high}`, `astra-{low,high}` | The shipped intelligence classes: one file per tier × thinking level, each mapping `anthropic` / `openai` / `codex` / `google` to a model and an effort setting. From the deep tier upward there is no `google` slice, and `astra-*` (OpenAI's strongest model) has only the OpenAI/Codex ones. | [reference/profiles-and-classes.md](../profiles-and-classes.md) |
| [`src/profiles/defaults/`](../../../src/profiles/defaults/) — worker templates | 2 — `worker-claude`, `worker-codex` | `template: true`: the role, rules, capabilities and harness every rung of that harness inherits through `extends`. Never synced to `agent_profiles`. The runnable workers are the derived `<class>-<harness>` rungs ([`src/profiles/catalog.py`](../../../src/profiles/catalog.py)). | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) |
| [`src/profiles/defaults/`](../../../src/profiles/defaults/) — stage profiles | 8 — `supervisor`, `triage`, `reviewer`, `final-reviewer`, `pr-merger`, `planner`, `playbook-compiler`, `spec-ingest` | Single-purpose profiles for pipeline roles. Never a router candidate. `triage`, `spec-ingest`, `reviewer` and `final-reviewer` are *roles*: a service or playbook may file a task on one (`route_source='role'`), and it runs the role's own class without being routed. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) |
| [`src/prompts/default_playbooks/default-assignment-routing.md`](../../../src/prompts/default_playbooks/default-assignment-routing.md) | 1 | The default router, and every project's binding unless it is re-bound (`routing.default_router`): the one `route-task` rule on `task.route_needed` (`task_route_plan`, an LLM classification only when the policy needs one, `task_route_apply`), the `## Routing policy` YAML block (kinds, lanes, reserved cells, balance weights) and the "Classifying a task" guidance. Its reviewed bundle is [`src/prompts/reviewed_playbooks/default-assignment-routing/`](../../../src/prompts/reviewed_playbooks/default-assignment-routing/). Owned by the `playbooks` shard; named here because it is where routing policy lives. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) |

## Focused tests

```bash
aq test tests/test_routing_mandatory.py tests/test_routing_planner.py \
        tests/test_routing_router.py tests/test_routing_enforcement.py \
        tests/test_routing_doctor.py tests/test_task_route.py \
        tests/test_task_route_source.py tests/test_task_routing_contract.py \
        tests/test_default_assignment_routing_playbook.py tests/test_explain.py
aq test tests/test_assignment_routing.py tests/test_agent_task_routing.py \
        tests/test_agent_flock.py tests/test_agent_liveness.py \
        tests/test_agent_subagents.py tests/test_agent_terminals.py \
        tests/test_agent_names.py tests/test_intelligence_classes.py \
        tests/test_intelligence_class_registry.py tests/test_intelligence_class_editing.py \
        tests/test_profile_parser.py tests/test_profile_sync.py \
        tests/test_capability_policy.py tests/test_profile_intelligence.py \
        tests/test_profile_drift.py \
        tests/test_retired_defaults.py tests/test_profile_migration.py \
        tests/test_profile_model_pin_migration.py tests/test_project_override_migration.py
```
