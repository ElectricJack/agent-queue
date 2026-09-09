# Module catalog — routing

Agents, profiles, intelligence classes and assignment routing: the 23
production modules that decide *which* worker runs a task and *how hard it
thinks*, plus the shipped markdown they read.

Prose for everything here lives on two pages:

* [Agents and routing](../../concepts/agents-and-routing.md) — the concepts,
  the routing flow, compatibility, failures and recovery.
* [Profile and class reference](../profiles-and-classes.md) — every field, the
  override precedence, worked examples and the operator commands.

Related shards: [sessions](sessions.md) owns launching the CLI a profile
selects; the scheduler shard owns pool sizing and the agent reconciler; the
[CLI shard](cli.md) owns the `aq agent` / `aq task route` command surface.

## The route

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/assignment_routing.py`](../../../src/assignment_routing.py) | States that the task row *is* the route: reads `tasks.intelligence_class`, decides nothing, and keeps the stub seam tests inject through. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Exists to document an absence — there is no routing-decision table. `tests/test_assignment_routing.py` |
| [`src/agents/routing.py`](../../../src/agents/routing.py) | The compatibility rules as one pure function: `task_agent_mismatch` returns `None` or the sentence saying why this worker cannot serve this task. Also the profile-resolution cascade. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | No I/O, no mutation; availability and capacity are the caller's problem. The generic worker ladder is matched by id prefix plus `harness == "claude"`, never an enumerated set. `tests/test_agent_task_routing.py` |

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
| [`src/profiles/default_selection.py`](../../../src/profiles/default_selection.py) | Picks a project's fallback `default_profile_id` deterministically, so a project with READY tasks and no profile cannot deadlock. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | The standard tier is named explicitly because alphabetical order picks the deep tier out of the shipped ladder. `tests/test_profile_default_selection.py` |
| [`src/profiles/drift.py`](../../../src/profiles/drift.py) | Compares each vault copy of a shipped profile against the in-tree default on the semantic `## Config` fields, and performs the explicit reseed with a backup. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) | Compares `read_only`, `harness`, `lifecycle`, `needs_workspace` only; everything else is operator tuning. Backs `aq agent profile-drift` / `profile-reseed` and `doctor --check profiles.system_drift`. `tests/test_profile_drift.py` |
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
| [`src/prompts/default_intelligence_classes/`](../../../src/prompts/default_intelligence_classes/) | 12 — `{fast,standard,deep}-{off,low,medium,high}` | The shipped intelligence classes: one file per tier × thinking level, each mapping `anthropic` / `openai` / `codex` / `google` to a model and an effort setting. | [reference/profiles-and-classes.md](../profiles-and-classes.md) |
| [`src/profiles/defaults/`](../../../src/profiles/defaults/) — worker ladder | 3 — `worker-standard-medium-claude`, `worker-deep-high-claude`, `worker-fast-medium-claude` | The shipped generic workers: `harness: claude`, `lifecycle: task`, one `default_class` each. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) |
| [`src/profiles/defaults/`](../../../src/profiles/defaults/) — stage profiles | 8 — `supervisor`, `triage`, `reviewer`, `final-reviewer`, `pr-merger`, `planner`, `playbook-compiler`, `spec-ingest` | Single-purpose profiles for pipeline roles. Never offered as an ordinary worker route, and never a project default. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) |
| [`src/prompts/default_playbooks/default-assignment-routing.md`](../../../src/prompts/default_playbooks/default-assignment-routing.md) | 1 | The routing **policy**: the one `route-task` rule and the "Choosing a class" guidance. Owned by the `playbooks` shard (planned); named here because it is where routing decisions actually live. | [concepts/agents-and-routing.md](../../concepts/agents-and-routing.md) |

## Focused tests

```bash
aq test tests/test_assignment_routing.py tests/test_agent_task_routing.py \
        tests/test_agent_flock.py tests/test_agent_liveness.py \
        tests/test_agent_subagents.py tests/test_agent_terminals.py \
        tests/test_agent_names.py tests/test_intelligence_classes.py \
        tests/test_intelligence_class_registry.py tests/test_intelligence_class_editing.py \
        tests/test_profile_parser.py tests/test_profile_sync.py \
        tests/test_capability_policy.py tests/test_profile_intelligence.py \
        tests/test_profile_default_selection.py tests/test_profile_drift.py \
        tests/test_retired_defaults.py tests/test_profile_migration.py \
        tests/test_profile_model_pin_migration.py tests/test_project_override_migration.py
```
