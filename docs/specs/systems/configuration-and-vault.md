# System spec: configuration and vault

Where an Agent Queue install keeps its settings and its markdown, and how both reach a
running daemon. This page covers:
- the operator config file `~/.agent-queue/config.yaml`: how it is loaded, validated,
  edited, reloaded, tuned, redacted and made portable;
- secrets, which are `${VAR}` references resolved from the environment or a `.env` file;
- the vault `~/.agent-queue/vault/`: its layout, the write-if-absent seeding of
  shipped content, the watcher that reacts to edits, and the index pages;
- the vault side-channels: facts files, project overrides, project READMEs and
  reference stubs;
- the prompt-building code that is not `aq prime`: the legacy `PromptBuilder`, the
  per-workspace prompt manager and the task templates.

This is system 13 of the 18 in the system inventory. It describes `main` as of
2026-10-20. Where a guide already explains something well, this page gives the short
version and links to it. Line numbers are a starting point for a reader; they drift.

**Much of this system is plumbing that other systems sit on, and a fair part of the
vault machinery is inert today.** The config stack is live and central. Every
setting a daemon reads arrives through `AppConfig`. The vault seeding is live. It is
how shipped profiles, harnesses, intelligence classes, formulas and templates reach
an install. But the memory plugin is switched off on every start (see
[Behaviour gaps](#behaviour-gaps)). As a result, several vault handlers only log:
- facts sync;
- overrides;
- memory projection.

`PromptBuilder` is no longer how agents get their prompt. `aq prime` is.

Start with [the configuration-and-vault concept page](../../concepts/configuration-and-vault.md)
for the operator's view and [the configuration reference](../../reference/configuration.md)
for the keys.

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

An Agent Queue install has two kinds of durable, operator-owned state that are not in
PostgreSQL:

1. **Settings.** These cover:
   - the database URL, ports and timeouts;
   - pool sizes, resource limits and provider policy;
   - Discord, the dashboard and dozens more.

   These live in one YAML file so that an operator can read, diff, back up and move
   them.
2. **Authored markdown.** These are:
   - profiles, intelligence classes and harness definitions;
   - formulas and task templates;
   - project notes, specs and memory exports.

   These live in an Obsidian-compatible folder, the vault, so that a human can edit
   them in any editor and an agent can read them with plain file tools.

This system solves five problems around that state:

- **One typed view of settings.** `load_config` turns layered YAML, environment
  overlays and secret references into a validated `AppConfig` dataclass tree. Bad
  config fails at startup with a named error, not later in a subsystem.
- **Changing settings without a restart where that is safe.** A config watcher applies
  "hot" sections to the live object and reports the rest as needing a restart.
- **Secrets that never sit in the YAML.** Credentials are `${VAR}` references. The
  config API redacts them on read and restores them on write.
- **Shipping defaults without clobbering edits.** Shipped vault content is written
  only if absent. A few narrow, explicit refresh paths cover cases where the shipped
  copy must win. Hash-matched harnesses, reviewed playbook bundles and additive
  capability grants are the only three.
- **Reacting to vault edits.** A polling watcher dispatches changed paths to handlers
  that reload profiles, harnesses, classes, formulas and MCP definitions without a
  restart.

## Vocabulary

| Term | Meaning |
|---|---|
| Operator config | `~/.agent-queue/config.yaml`, plus an optional sibling `.env`, environment overlay and profile overlay. |
| `AppConfig` | The root dataclass in [src/config.py](../../../src/config.py) (about line 3887). It has 58 top-level sections. |
| Section | A top-level key such as `scheduling`, `database` or `providers`. `config_section_names()` lists them. |
| Hot section | A section in `HOT_RELOADABLE_SECTIONS`. A changed value is applied to the live config without a restart. |
| Restart-required section | A section in `RESTART_REQUIRED_SECTIONS`. A change is reported (`config.restart_needed`) but not applied. |
| Environment overlay | `config.<env>.yaml`, where `<env>` comes from `AGENT_QUEUE_ENV`, then the file's own `env:` key, then `production`. |
| Profile overlay | `profiles/<name>.yaml` beside the config, chosen by argument or `AGENT_QUEUE_PROFILE`. This is unrelated to agent profiles. |
| Secret reference | A `${NAME}` string in the YAML. It is resolved from the process environment at load time. |
| Redaction placeholder | `__aq_redacted__`, the value the config API returns in place of a secret. |
| Portable bundle | An `.aqbundle` archive (`aq-portable-config` version 2) that carries non-personal settings between installs. |
| Vault | `~/.agent-queue/vault/`, the markdown tree. |
| Write-if-absent | The seeding rule: a shipped file is copied into the vault only when no copy exists. |
| Hub file | An index page `<dir>/<dir>.md`, generated by [src/vault_index.py](../../../src/vault_index.py) for Obsidian navigation. |
| Facts file | `facts.md` at system, agent-type or project scope. It holds key/value pairs meant for the memory plugin's KV store. |
| Override | `projects/<pid>/overrides/<agent-type>.md`, project-specific guidance meant for an agent type. |
| Reference stub | `projects/<pid>/references/spec-<name>.md`, a summary stub of a spec file found in a workspace. |

## How it works today

### The big picture

```
                 config.yaml  (+ .env, config.<env>.yaml, profiles/<p>.yaml)
                      │
                load_config()  ── ConfigValidationError on bad input
                      │
                  AppConfig  ──────────────► every subsystem reads its section
                      ▲
   ConfigWatcher ─────┘  (mtime poll; hot sections setattr'd, others → restart_needed)
   aq system config set / update_config ─► write YAML ─► reload

   src/prompts/, src/profiles/defaults/, default harnesses, skills
                      │  ensure_vault_layout()  (write-if-absent, three refresh exceptions)
                      ▼
                ~/.agent-queue/vault/  ◄── operator and agents edit markdown
                      │
               VaultWatcher.check()   (called each orchestrator tick)
                      │
     handlers: profiles · harnesses · classes · formulas · MCP · workspace kinds
               facts* · overrides* · READMEs · index pages · spec stubs
                                         (* inert while the memory plugin is off)
```

The config side is synchronous and happens before anything else. The orchestrator
cannot be built without an `AppConfig`. The vault side runs during orchestrator
startup (`_ensure_vault_structure`) and then on every tick through the watcher.

### Loading the config

`load_config(path, profile=None)` ([src/config.py](../../../src/config.py) about line
4866) runs these steps in order:

1. **Read the `.env` file** beside the config (`_load_env_file`, about line 4686).
   Values from the file **override** the process environment. When they differ, the
   loader logs a warning (issue #30: a stale shell export must not beat the file the
   installer wrote). The installer writes this file with mode 0600
   ([src/install/postgres_steps.py](../../../src/install/postgres_steps.py) about line
   930).
2. **Parse the YAML** with `yaml.safe_load`.
3. **Apply the environment overlay** `config.<env>.yaml` if it exists.
4. **Apply the profile overlay** `profiles/<p>.yaml`. A named profile whose file is
   missing raises `FileNotFoundError`.
5. **Merge** with `_deep_merge` (about line 4608). Mappings merge recursively. Lists
   replace. An explicit `null` deletes the key.
6. **Check GitHub App settings** (`validate_github_app_raw_config`, about line 2526).
   This runs before and after substitution, so a reference cannot smuggle in an
   invalid shape.
7. **Substitute secrets.** Every `${NAME}` string is replaced from the environment
   (`_substitute_env_vars`, `_process_values`). An unset variable raises
   `ValueError`. A bare `$NAME` is left alone.
8. **Map sections to dataclasses.** Newer sections go through `_dataclass_kwargs`
   (about line 4822), which takes the dataclass's own fields and defaults. About 22
   older sections still use hand-written `.get(key, default)` mappers. Each of those
   repeats the dataclass default, and the two copies can drift: a past incident
   dropped 54 fields. `tests/test_config_section_roundtrip.py` is the guard.
9. **Validate** with `AppConfig.validate()` (about line 4021). It:
   - rejects SQLite URLs (about line 4111);
   - checks that the dashboard and MCP ports differ;
   - checks that `supervisor_agent` has `messages` and `sessions` enabled;
   - runs many range checks.

   Failures raise `ConfigValidationError` with every problem listed.

Unknown top-level keys are ignored without a warning. Three retired keys are handled
specially:
- `pause_retry` is dropped with a warning;
- `hook_engine` is dropped silently;
- `chat_provider` is accepted as an alias for `llm`.

There are **no per-project config files**. Only three settings are keyed by project or
repository:
- `project_roots`;
- `resources.test_interpreters`;
- the per-repository timeouts under `worktrees`.

Everything else per project lives in the database (the `projects` table and its
policy columns) or in the vault (`projects/<pid>/`).

### Hot reload and restart

[src/config.py](../../../src/config.py) declares two disjoint sets that together cover
all 58 sections:

- **Hot (31)** (`HOT_RELOADABLE_SECTIONS`, about line 4320). These are:
  - `scheduling`, `monitoring`, `archive`, `llm_logging`, `agents_config`,
    `auto_task`, `logging`;
  - `agent_profiles`, `global_token_budget_daily`, `max_daily_playbook_tokens`,
    `max_concurrent_playbook_runs`;
  - `rate_limits`, `state_machine`, `work_graph`, `swarm`, `resources`,
    `test_selection`, `metrics`;
  - `providers`, `provider_failover`, `routing`, `docs`, `graph_layout`,
    `dashboard_server`, `host_shell`, `pricing`;
  - `surface`, `project_roots`, `git_identity`, `deploy`, `knowledge`.
- **Restart required (27)** (`RESTART_REQUIRED_SECTIONS`, about line 4373). These are:
  - `database`, `database_path`, `data_dir`, `workspace_dir`, `env`, `profile`;
  - `discord`, `messaging_platform`, `mcp_server`, `api_auth`, `security`;
  - `llm`, `memory`, `memory_extractor`, `messages`, `inbox`, `events`,
    `streams`, `validate_events`;
  - `sessions`, `supervisor`, `supervisor_agent`, `worktrees`, `integration`,
    `playbooks`, `health_check`, `reports`.

`ConfigWatcher` (about line 4441) polls the file's mtime every
`scheduling.config_poll_interval_seconds` (default 30). On a change it reloads,
diffs with `diff_configs` (about line 4422), assigns each changed hot section onto the
live `AppConfig` with `setattr`, and emits `config.reloaded`. Changed
restart-required sections produce `config.restart_needed` instead. The orchestrator
builds the watcher (about line 2641 of
[src/orchestrator/core.py](../../../src/orchestrator/core.py)) and reacts in
`_on_config_reloaded` (about line 4571).

Hot reload is only as good as the reader. A component that copied its sub-config
into a field at construction keeps the old values. It sees the change only if it
reads `self.config.<section>` each time. `aq system reload-config`
(`_cmd_reload_config`, [src/commands/system_commands.py](../../../src/commands/system_commands.py)
about line 522) forces the same path and also reloads intelligence classes.

### Editing the config

All writes go through [src/config_editor.py](../../../src/config_editor.py), which uses
ruamel in round-trip mode so that comments and key order survive. Its parts:
- **Readers:**
  - `read_raw_config` returns the file without substitution;
  - `find_env_var_refs` lists the `${VAR}` names;
  - `classify_sections` tags each section hot or restart.
- **Schema:** `build_config_schema` (about line 312) produces a JSON Schema with an
  `x-reload` hint per section. It strips the machine-specific defaults of `data_dir`
  and `workspace_dir`.
- **Writers:**
  - `write_section` (about line 360) replaces one section;
  - `update_section_keys` (about line 394) merges keys into one;
  - `write_full_config` replaces the file.

  `_quote_yaml11_scalars` quotes strings such as `on` or `no` that YAML 1.1 would read
  as booleans.

The commands in [src/commands/system_commands.py](../../../src/commands/system_commands.py)
wrap these:

| Command | Line (about) | What it does |
|---|---|---|
| `get_config` | 251 | Returns the raw config, redacted. |
| `get_config_schema` | 312 | Returns the schema with reload hints. |
| `update_config` | 393 | Restores redacted secrets, writes one section, validates by reloading, and restores the old file on failure. |
| `reload_config` | 522 | Re-reads the file now. |
| `preview_portable_config` / `export_portable_config` / `import_portable_config` | 325 / 340 / 360 | See [Portable bundles](#portable-bundles). |

The CLI is `aq system config` ([src/cli/system_config.py](../../../src/cli/system_config.py)):
- `get [SECTION]`;
- `set KEY=VALUE [--dry-run]`;
- `edit` (opens `$EDITOR` on a section);
- `schema`;
- `tune`;
- `git-identity`.

The dashboard's settings page uses the same commands through the API.

### Secrets

There is no keyring. [src/config_secrets.py](../../../src/config_secrets.py) handles
both ends of the secret path:
- **Storage:** a secret lives in the environment or in `~/.agent-queue/.env`, and the
  YAML holds `${VAR}`.
- **Redaction on read:** `redact_config` replaces values with `__aq_redacted__`. It
  redacts a value when either test matches:
  - the key name matches a secret pattern (`token`, `password`, `secret`, `api_key`
    and so on);
  - the value looks like a credential: `sk-…`, `ghp_…`, `xox…`, `AKIA…`, `AIza…`,
    PEM blocks, or URLs with embedded credentials.
- **Restore on write:** `restore_section_secrets` puts the original values back
  wherever the incoming section still holds the placeholder. A client can therefore
  round-trip a section without ever seeing a secret.

The design is
[2026-09-10-config-api-secret-redaction-design.md](../../superpowers/specs/2026-09-10-config-api-secret-redaction-design.md).

### Machine tuning

[src/config_tuning.py](../../../src/config_tuning.py) recommends settings from the
hardware. The steps are:

1. `MachineResources` (about line 56) measures CPU count, memory and disk.
2. `recommended_tuning` (about line 159) derives:
   - pool sizes;
   - test-slot counts;
   - resource gates;
   - database pool limits.
3. `DERIVED_KEYS` (about line 697) names the keys tuning owns.
4. `tuning_plan` (about line 760) classifies each key as add, keep, replace or
   unchanged.
5. `apply_tuning` (about line 789) writes the result.

`aq system config tune [--apply]` shows or applies the plan. Onboarding runs it during
install ([src/install/onboarding.py](../../../src/install/onboarding.py) about lines 381
and 397). [default-tuning.md](../../guides/default-tuning.md) explains the formulas.

### Portable bundles

[src/portable_config.py](../../../src/portable_config.py) carries a setup between
installs. [portable-config.md](../portable-config.md) is the behaviour spec. In short:
- **Contents.** A version-2 `.aqbundle` holds:
  - `manifest.json` and `config.yaml`;
  - `profiles/` and `intelligence-classes/`;
  - `setup.json`, which carries pool state and project and repository policy.
- **What it carries.** `PORTABLE_CONFIG_SECTIONS` (about line 32) lists what travels.
  `LOCAL_CONFIG_FIELDS` (about line 68) names fields that never travel:
  - paths;
  - endpoints;
  - account identities.
- **Unknown credentials.** `_assert_portable_value` (about line 123) fails on a
  credential-shaped value it does not recognise, rather than exporting it.
- **Reading a bundle.** `read_bundle` (about line 362):
  - limits each member to 2 MB and the archive to 8 MB;
  - rejects traversal, symlinks and members it does not expect.
- **Importing.** `import_bundle` (about line 478) offers three modes: keep the
  existing value, replace it, or raise an error on conflict. `bundle_preview` (about
  line 279) is the dry run.

### The vault layout

[src/vault.py](../../../src/vault.py) owns the tree. Static directories
(`_STATIC_DIRS`, about line 1328):
- `.obsidian/`, `system/`, `templates/` and `harnesses/`;
- `agent-types/`, including `agent-types/supervisor/playbooks/` and
  `agent-types/supervisor/memory/`.

Per-profile directories come from `ensure_vault_profile_dirs` (about line 2085).
Per-project directories come from `ensure_vault_project_dirs` (about line 2299):

```
projects/<pid>/
  memory/{knowledge,insights,guidance}/
  playbooks/  notes/  specs/  references/  overrides/
```

Other shipped content lands in sibling folders. These are intelligence classes,
formulas, task templates (`templates/tasks/`) and the reviewed playbook bundles.
[docs/reference/modules/vault.md](../../reference/modules/vault.md) has a
folder-by-folder table. Note the folder corrections listed under
[Known gaps](#where-the-docs-and-the-code-disagree).

### Seeding shipped content

`ensure_vault_layout` (about line 1348) runs on every daemon start, in this order:

1. **Install the initial playbooks**, first install only (`install_initial_playbooks`,
   [src/playbooks/required.py](../../../src/playbooks/required.py) about line 60). A
   receipt `playbook-installation.json` stops it from running again.
2. **Create directories.**
3. **Copy templates**, including the 72 task templates from `src/prompts/task_templates/`
   (about line 1417).
4. **Seed harnesses** from `src/sessions/default_harnesses/` (about line 1657).
5. **Seed formulas** from `src/prompts/formulas/` (about line 1607).
6. **Seed intelligence classes** from `src/prompts/default_intelligence_classes/`
   (about line 1625). A `.retired` marker stops a class the operator removed from
   coming back.
7. **Seed profiles** (`ensure_default_profiles`, about line 1782). There are 13
   shipped ids. A `.retired-defaults` tombstone records ones the operator removed.
8. **Seed aq skills** (`ensure_default_aq_skills`, about line 1686). These go to
   `~/.claude/skills`, `~/.gemini/skills` and `~/.codex/skills`, not to the vault.
9. **Regenerate the hub index pages** (`VaultIndexGenerator.generate_all()`).

The rule for every step is **write-if-absent**. An installed copy is never overwritten,
so a fix to a shipped profile or skill changes nothing on an existing install. Three
paths deliberately refresh installed content:

- **Harnesses** are refreshed when the installed file's hash matches an *older shipped*
  version ([src/sessions/harness_manifest.py](../../../src/sessions/harness_manifest.py)).
  An operator-edited harness is left alone. `aq vault reset-harness` replaces it on
  request.
- **Reviewed playbook bundles** (`ensure_reviewed_playbook_bundles`,
  [src/playbooks/required.py](../../../src/playbooks/required.py) about line 210) are
  replaced by the shipped bundle. The old copy is kept as `.bak-<epoch>`.
- **Capability grants** on the `supervisor` and `worker-*` templates are merged in
  additively ([src/profiles/capability_sync.py](../../../src/profiles/capability_sync.py)).
  This happens on startup (core.py about lines 2730-2746) and on each profile reload
  ([src/profiles/sync.py](../../../src/profiles/sync.py) about line 494). The profile
  frontmatter `capability_sync: false` opts a profile out.

The gap between shipped and installed content is visible but not repaired
automatically:
- `aq doctor --check profiles.system_drift` and
  `aq doctor --check skills.installed_drift` name it (logic in
  [src/profiles/drift.py](../../../src/profiles/drift.py));
- `aq agent profile-drift` shows a diff;
- `aq agent profile-reseed --profile-id <id> [--grants-only]` repairs a profile, and
  `--grants-only` merges grants without touching operator text;
- the skills doctor fix writes `SKILL.md.bak` before replacing a skill.

### Startup work on the vault

The orchestrator creates the `VaultManager` (about line 2029 of
[src/orchestrator/core.py](../../../src/orchestrator/core.py)) and calls
`_ensure_vault_structure` (defined about lines 3056-3218). That method does the
following:
- runs the layout and seeding above;
- reloads intelligence classes;
- runs `run_vault_migration` ([src/vault.py](../../../src/vault.py) about line 2686),
  but only when legacy data exists and the vault is empty. Operators can run the same
  migration with `aq vault migrate [--dry-run]`;
- retires project-scoped profiles;
- migrates profile rows from the database into vault markdown;
- moves old passive rules into memory;
- registers each project and agent type.

### The vault watcher

[src/vault_watcher.py](../../../src/vault_watcher.py) detects edits by polling. How it
runs:
- **Construction.** The orchestrator creates one (core.py about line 2046) with a
  5-second poll, a 2-second debounce and a 30-second maximum pending age.
- **Ticking.** It never calls `start()`. Instead, each orchestrator tick calls
  `check()` (about line 3550).
- **The first check** only takes a snapshot.
- **Later checks** compare mtime and size inside `asyncio.to_thread`. There is no
  inotify.
- **Changes** are collected, deduplicated (`_flush_pending`, about line 425) and
  dispatched to every handler whose pattern matches (`_dispatch`, about line 488). A
  handler that raises is logged and does not stop the others.

Registered handlers:

| Pattern | Registered (core.py, about) | Handler | Effect today |
|---|---|---|---|
| `agent-types/*/profile.md` | 2059 | [src/profiles/sync.py](../../../src/profiles/sync.py) | Reparses the profile, upserts the row, runs capability sync. Live. |
| MCP server definitions | 2077 | MCP registry | Reloads the registry. Live. |
| `harnesses/*` | 2086 | harness loader | Reloads harness specs. Live. |
| intelligence classes | 2091 | class registry | Reloads classes. Live. |
| formulas | 2098 | formula loader | Reloads formulas. Live. |
| workspace kinds | 2108 | workspace-kind registry | Reloads kinds. Live. |
| `system/facts.md`, `agent-types/*/facts.md`, `projects/*/facts.md` | 2124 | [src/facts_handler.py](../../../src/facts_handler.py) | Would sync pairs into the memory KV store. With no memory service it only logs. |
| `**/*.md` | 2126 | vault-index updater | Regenerates the hub page of the changed directory. Live. |
| `projects/*/overrides/*.md` | 2444 | [src/override_handler.py](../../../src/override_handler.py) | Would re-index the override for retrieval. No indexer is ever attached, so it only logs. |
| `projects/*/README.md` | 2455 | [src/readme_handler.py](../../../src/readme_handler.py) | Copies a deterministic summary to `agent-types/supervisor/memory/project-<pid>.md`. A startup scan (about line 2794) catches up. Live. |
| workspace `specs/` files | 2470 | spec watcher | Writes `projects/<pid>/references/spec-<name>.md` stubs. Only when `memory.enabled` and `spec_watcher_enabled`. |
| reference stubs | 2496 | [src/reference_stub_enricher.py](../../../src/reference_stub_enricher.py) | Fills a stub's summary with a direct model call. It caps the source at 20,000 characters and skips a stub whose `source_hash` is unchanged. Also gated on `stub_enrichment_enabled`. |

Two kinds of vault content are deliberately **not** watched:
- **Playbook markdown.** V2 playbooks change only through commands and review
  (`RequiredPlaybookReconciler`, the V2 runtime). Editing a file in the vault does not
  activate it.
- **Knowledge records.** These flow one way, from PostgreSQL out to the vault, through
  the record exporter (core.py about line 2430). The vault holds an export of them,
  not their source. See [memory-plugin.md](../design/memory-plugin.md).

### Index pages

[src/vault_index.py](../../../src/vault_index.py) writes Obsidian hub pages:
- `generate_all` (about line 122) runs at startup;
- `update_directory` (about line 309) runs from the watcher.

A hub file is `<dir>/<dir>.md` (`_is_hub_file`, about line 93). It lists the
directory's notes and subdirectories with wikilinks. `aq system vault-rebuild-index`
(`_cmd_vault_rebuild_index`, system_commands.py about line 1417) regenerates
everything.

### Prompts and templates

The prompt an agent works from is assembled by **`aq prime`**
([src/prime/renderer.py](../../../src/prime/renderer.py)). That belongs to
sessions-and-harnesses (system 5). It reads this system's vault content:
- profile markdown, from which it keeps the `Role` and `Rules` headings;
- the project role file `projects/<pid>/agent-types/<id>/profile.md`;
- a workspace's `.aq/PRIME.md` override.

This system still owns three older template engines:

- **`PromptBuilder`** ([src/prompt_builder.py](../../../src/prompt_builder.py)) is the
  layered prompt assembler from before prime. It has these parts:
  - an identity tier L0 (400 tokens);
  - facts L1 (200);
  - guidance (300);
  - context L2 (500).

  It warns at twice a tier's budget and does lenient `{{var}}` substitution. Only
  `extract_section` is used in production, by
  [src/prime/sections.py](../../../src/prime/sections.py). The orchestrator's
  `_build_task_context_with_prompt_builder` has had no caller since commit 533bda271
  (2026-09-01).
- **The prompt manager** ([src/prompt_manager.py](../../../src/prompt_manager.py))
  serves per-workspace templates from `<workspace>/prompts/`. It supports optional
  strict variables. Only the `list_prompts`, `read_prompt` and `render_prompt`
  commands use it (system_commands.py about lines 1286-1414).
- **Task templates** ([src/task_templates.py](../../../src/task_templates.py)) render
  the 72 shipped task bodies with strict variables. The vault copy under
  `templates/tasks/` wins over the packaged one.

Bootstrap prompts are constants in
[src/sessions/spec.py](../../../src/sessions/spec.py) (`BOOTSTRAP_PROMPT`,
`POOL_BOOTSTRAP_PROMPT`). They are not templates.

What in `src/prompts/` is live:

| Path | Status |
|---|---|
| `default_intelligence_classes/` (7), `formulas/` (2), `task_templates/` (72) | Seeded into the vault, write-if-absent. |
| `reviewed_playbooks/` (15 bundles) | Refreshed into the vault (owned by playbooks, system 8). |
| `integration_playbooks/`, `project_playbooks/` | Authoring sources only. |
| `default_playbooks/` | Inventory only. |
| `consolidation_task.md` | Historical. Reachable only through the `aq://prompts` resource. |
| `supervisor_system.md`, `execution_focus.md`, `plan_structure_guide.md`, `memory_consolidation.py`, `memory_revision.py` | Not loaded by production code. |

## Data model

This system has no PostgreSQL tables of its own. Its state is files:

| Store | Location | Written by | Read by |
|---|---|---|---|
| Operator config | `~/.agent-queue/config.yaml` | the installer, `aq system config`, `update_config`, `tune --apply`, a hand editor | `load_config`, `ConfigWatcher` |
| Overlays | `config.<env>.yaml`, `profiles/<p>.yaml` beside it | the operator | `load_config` |
| Secrets | the environment and `~/.agent-queue/.env` (0600) | the installer, the operator | `_load_env_file`, `${VAR}` substitution |
| Backup during `update_config` | `config.yaml.bak` | `update_config` | `update_config` (on failure). Deleted afterwards. |
| Vault tree | `~/.agent-queue/vault/` | seeding, handlers, record export, agents, the operator | the watcher, prime, the profile, class, harness and formula loaders |
| Seeding receipts | `playbook-installation.json`, `.retired`, `.retired-defaults` | seeding, the operator | seeding |
| Harness manifest | shipped hashes in `src/sessions/harness_manifest.py` | release | harness refresh, `harness.drift` doctor check |
| aq skills | `~/.claude/skills`, `~/.gemini/skills`, `~/.codex/skills` | `ensure_default_aq_skills` | the harness CLIs |
| Config schema | [docs/reference/configuration-schema.json](../../reference/configuration-schema.json) | `scripts/generate-config-schema-inventory.py` | docs, the drift check |

The profile, intelligence-class and MCP rows in PostgreSQL are projections of vault
markdown. Routing-and-profiles (system 4) owns them. The vault file is the authority,
and the watcher keeps the row in step.

## Boundaries

| Neighbour | What it owns | Where it meets this system |
|---|---|---|
| `daemon-core` | Startup, `run_one_cycle`, the event bus, schema and migrations | Startup calls `load_config`, builds `ConfigWatcher` and runs `_ensure_vault_structure`. The tick calls `VaultWatcher.check()`. `config.reloaded` and `config.restart_needed` travel on its bus. |
| `task-graph` | Tasks, hierarchy, formulas' runtime | Formula markdown is seeded and watched here. Task templates render task bodies. |
| `scheduling-and-claims` | The frontier, pools, claims | Reads `scheduling`, `swarm`, `agent_profiles` and `resources`, all hot sections. |
| `routing-and-profiles` | Profile parsing, derived rungs, intelligence classes, routing | Profile and class markdown are seeded and watched here. Capability sync and drift checks run from here. Parsing and the database rows are theirs. |
| `sessions-and-harnesses` | tmux sessions, harness specs, `aq prime` | Harness files are seeded, hash-refreshed and watched here. Prime reads vault profile text and uses `extract_section`. `sessions` config is restart-only. |
| `projects-workspaces-git` | Projects, workspaces, worktrees | `ensure_vault_project_dirs` runs at project registration. `worktrees` and `project_roots` config. Workspace-kind definitions are watched. The spec watcher reads workspace `specs/`. |
| `integration-and-delivery` | Trains, batches, promotion | The `integration` section (restart only) and `deploy` (hot). Delivery policy lives in the database, not in config. |
| `playbooks` | The V2 runner, required playbooks, reviewed bundles | First-install playbook installation and bundle refresh run during seeding. Playbook markdown is not watched. |
| `messaging-and-human-asks` | Messages, Discord, escalations | `discord`, `messages`, `inbox` and `messaging_platform` config, all restart only. |
| `supervisors-and-policy` | Supervisor profile, policy profiles | Capability sync of the supervisor template. The README handler writes supervisor memory pages. Policy profiles export project policy. Portable bundles export install settings. |
| `reviews-and-knowledge` | Reviews, knowledge records, the memory plugin | Record export writes into the vault. Facts and overrides would feed the memory plugin, which is off. Review documents live under `projects/<pid>/specs/`. |
| `providers` | Availability and failover | `providers`, `provider_failover` and `pricing` are hot sections. |
| `api-and-clients` | Routes, tokens, generated clients | Config get, set and schema routes call the commands here. Redaction protects them. `api_auth` is restart only. |
| `dashboard` | Operator UI | The settings page edits config through `update_config` and reads the schema. `dashboard_server` is hot. |
| `operations-and-recovery` | Install, update, doctor, restart | The installer writes `config.yaml` and `.env` and runs tuning. Doctor checks `config.parse`, `vault.parse`, `profiles.*`, `skills.installed_drift` and `harness.drift`. `aq update` reseeds nothing: write-if-absent applies. |
| `plugins-mcp-and-tools` | Plugins, MCP server, aq skills content | Skills are seeded here. MCP server definitions are watched here. `mcp_server` config is restart only. |
| `test-service-and-jobs` | `aq test`, slots | Reads `resources.test_interpreters` and `test_selection`, both hot. |

## Design principles and invariants

These hold in the code today. A change that breaks one needs a spec.

1. **PostgreSQL only.** `validate()` refuses a SQLite URL. No config path selects
   another database.
2. **Secrets never sit in the YAML.** They are `${VAR}` references. The config API
   never returns a resolved secret, and a write that carries the placeholder keeps the
   stored value.
3. **Load fails loudly.** An unset `${VAR}`, a missing named profile overlay or an
   invalid value stops startup with a named error. There is no silent fallback to
   defaults for a value the operator wrote. The one leniency is that unknown top-level
   keys are ignored.
4. **Every section is either hot or restart-required.** The two sets are disjoint and
   together equal `config_section_names()`. Adding a section means choosing one.
5. **Shipped vault content is write-if-absent.** Seeding never overwrites an installed
   copy. Only three paths refresh installed content, and each is narrow and
   reversible:
   - hash-matched harnesses;
   - reviewed bundles, with a backup;
   - additive grants, with an opt-out.
6. **Removal is remembered.** `.retired` and `.retired-defaults` stop a removed class or
   profile from being reseeded.
7. **The vault file is the authority for authored definitions.** Profile, class,
   harness, formula and MCP rows follow the markdown, not the reverse.
8. **The vault is not the authority for knowledge or playbook activation.** Knowledge
   records are exported to it. Playbook files in it are not activated by being edited.
9. **Handler failures are isolated.** One handler's exception does not stop others or
   the tick.
10. **State changes go through `CommandHandler`.** Config writes from the API, the
    dashboard and `aq system config set` use the commands in `system_commands.py`. The
    one exception is listed under [Behaviour gaps](#behaviour-gaps).

## Known gaps

### Where the docs and the code disagree

The code is right in each case.

- **`.env` precedence is documented backwards.**
  [config.md](../config.md) (§3 about line 57 and §5.6 about lines 981-987) and
  [configuration.md](../../reference/configuration.md) (about line 13) say the shell
  environment wins over `.env`. `_load_env_file` does the opposite: the file
  overrides and warns.
- **`update_config` deletes its backup.** [config.md](../config.md) §7 says the `.bak`
  is preserved. The code removes it in a `finally` block. The comment above it ("Take a
  backup so we can restore if the live reload chokes") is accurate only for the
  duration of the call.
- **The CLI syntax in config.md §7 is wrong.** It shows
  `aq system config --section …` and `set logging.level DEBUG`. The real forms are
  `get [SECTION]` and `set KEY=VALUE`. Its "plugin enable list" command does not exist.
- **config.md §4.5 documents `PauseRetryConfig`.** It does not exist. `pause_retry` is
  dropped with a warning.
- **The reload table in [configuration.md](../../reference/configuration.md) (about
  line 396) is incomplete.** It omits five hot sections: `test_selection`,
  `host_shell`, `git_identity`, `deploy` and `knowledge`. It also omits the
  restart-only `reports`.
- **[design/vault.md](../design/vault.md) describes a different tree.** It shows
  `vault/orchestrator/`, `agent-types/{coding,code-review,qa}` and
  `system/playbooks`. It places project overrides at
  `projects/<pid>/agent-types/<type>/profile.md`; that path is actually prime's
  *project role* file, and overrides live in `projects/<pid>/overrides/`. It mentions
  memsearch and `milvus.db`. It omits harnesses, formulas, intelligence classes and
  reviewed playbooks. Read it as history.
- **[docs/reference/modules/vault.md](../../reference/modules/vault.md)** says the
  repository's `vault/templates/*` files are seeded and that `supervisor_system.md` is
  loaded by `PromptBuilder`. Nothing seeds those templates, and nothing loads that
  prompt.
- **[configuration-and-vault.md](../../concepts/configuration-and-vault.md) (about line
  170)** describes `src/file_watcher.py` as live. It is never constructed.
- **[prompt-builder.md](../prompt-builder.md)** describes `platforms/claude_sdk`, a
  chat agent and a strict mode. It is stale throughout. The real assembler is prime.
- **`prompt_manager.py`'s docstring** cites `specs/prompt-system.md`, which does not
  exist.
- **The `VaultWatcher` docstring** says `*` does not cross `/`. The matcher uses
  `fnmatch` for patterns without `**`, and there `*` does cross `/`. For example,
  `projects/a/b/overrides/c.md` matches `projects/*/overrides/*.md`.
- **A comment points to `aq vault rebuild-index`.** The command is
  `aq system vault-rebuild-index`.

### Stale code and comments

- **Dead seeders in [src/vault.py](../../../src/vault.py):**
  - `ensure_default_playbooks` (about line 1505);
  - `ensure_default_agent_type_playbooks` (about line 1873), whose source directory is
    gone;
  - `ensure_supervisor_profile`, `ensure_claude_code_profile`,
    `ensure_claude_opus_profile` and `ensure_claude_sonnet_profile` (about lines
    1941-2037);
  - `ensure_shared_claude_memory_dir` (about line 2067);
  - `vault_has_profile_markdown` (about line 2424).
- **Unreachable branch.** The `elif all_profiles` branch in `_ensure_vault_structure`
  (core.py about line 3191) can never run.
- **Unused `VaultWatcher` code.** `start()` and `_poll_loop` are dead, because the tick
  drives `check()`. The module docstring describes the background loop.
- **Unused `VaultManager` getters.** They are used only in tests. Production uses
  `ensure_layout`, `register_project` and `register_agent_type`.
- **Dead or test-only modules:**
  - `src/wiki_links.py`: only `add_see_also` remains, used by the dead
    `migrate_backlinks` in `vault_index.py` (about line 560);
  - `src/vault_glossary.py`: no importers, and `vault/glossary/` is never created;
  - `src/file_watcher.py`: never constructed;
  - in `src/facts_parser.py`, everything except `parse_facts_file`.
- **Dead `PromptBuilder` path.** `PromptBuilder` and the orchestrator's
  `_build_task_context_with_prompt_builder` (context.py about lines 179-410) are dead.
  So are the `TaskContext` tier fields in `models.py` (about lines 1106-1113).
- **Unused `src/prompts/` files:** `supervisor_system.md`, `execution_focus.md`,
  `plan_structure_guide.md`, `memory_consolidation.py` and `memory_revision.py`.
- **Config leftovers:**
  - the `PerProjectChannelsConfig` shim (about line 127);
  - the `chat_provider` alias;
  - `database_path`, documented as "removed next release" but still accepted (about
    line 3908);
  - `KnowledgeConfig.legacy_memory_mode`;
  - `check_deprecations()` (about line 4249), which always returns an empty list;
  - `reload_non_critical()` (about line 4254), which has no production caller and a
    section list that has drifted from the real sets.

  The `DatabaseConfig` docstring (about line 1469) still shows a `.db` URL.
- **Stale text in `config_editor.py`.** Its "step 2" docstring and the `SECTION_NOTES`
  text "Disabled until the lane lands" describe finished work.
- **`vault/system/` is half retired.** config.py (about line 3992) maps the old
  `vault_system` location to `agent-types/supervisor`. Yet seeding still creates
  `system/`, and the facts handler still watches `system/facts.md`.

### Behaviour gaps

- **The memory plugin is always skipped, so three vault paths are inert.** The plugin
  loader skips `LEGACY_MEMORY_PLUGINS = {"aq-memory", "memory"}`
  ([src/knowledge/legacy.py](../../../src/knowledge/legacy.py)) on every start (core.py
  about line 2558). It logs "L1/L2 prompt tiers empty; reflection off". The pause is
  explained in [feature-pauses.md](../design/feature-pauses.md). As a result:
  - facts files sync nowhere;
  - `OverrideIndexer` is never built and `set_indexer` is never called, so editing an
    override changes nothing an agent sees;
  - prime's L1 and L2 sections always render empty.

  An operator who writes `facts.md` or `overrides/*.md` today gets no effect.
- **Facts sync is one way and never deletes.** When it ran, deleting a facts file or
  key left the KV entries in place.
- **`tune --apply` bypasses `CommandHandler`.** It writes the file directly from the
  CLI. It does not get `update_config`'s validate-and-restore protection, and no
  command event is recorded. `_detect_memory_gb` (config_tuning.py about line 122)
  uses a synchronous `subprocess.run` on macOS. AGENTS.md forbids that in production
  code.
- **`write_section` is not atomic.** It opens the file with `"w"` and writes. A crash
  mid-write leaves a truncated config. `update_config` holds a backup only for the
  duration of the call.
- **`update_config` does not check the section name.** A typo creates a new top-level
  key, and `load_config` then ignores it silently.
- **Secret restore matches list items by position.** `restore_section_secrets` pairs
  list elements by index. If a client reorders a list that contains redacted items,
  the secrets land on the wrong element.
- **Hot reload misses cached readers.** Any component that stored a sub-config at
  construction keeps old values after a hot reload. No test or registry lists which
  components read live.
- **`aq system config edit` loses comments inside sections.** `schema --json` defaults
  to true, so the flag has no effect.
- **Portable config and update validation ignore overlays.** `PORTABLE_CONFIG_SECTIONS`
  lists `planner` and `plugins`, which `load_config` never reads.
  `validate_candidate_config` (portable_config.py about line 454) says it applies
  overlays. It validates through a temporary file whose stem is wrong for the
  environment overlay and passes no profile. `update_config` has the same blind spot.
  A change that is valid alone but invalid under the overlay passes validation.
- **Hub page detection is by name.** `_is_hub_file` treats any `<dir>/<dir>.md` as an
  index. A real note `notes/notes.md` would be overwritten by `_generate_hub_for_dir`,
  which writes unconditionally. The loaders do not filter hub files. They work only
  because those directories hold few files.
- **The generated schema has no CI drift check.** `scripts/regenerate-generated.sh
  --check` covers `configuration-schema.json`, which `.gitattributes` (line 23) marks
  `merge=aq-generated`. The check is clean today, but no GitHub workflow runs it.
- **There are no per-project config overlays.** A setting that should differ by project
  has to become a database column or a vault file. The planned `wait_policy` per-project
  map is the first config-level exception (see below).

## Planned work

This is a snapshot of open tasks, re-queried on 2026-10-20. The inventory listed "Open
epics: none" for this system, and that is still true: no open epic is *about*
configuration or the vault. Several open epics change settings, shipped vault
content or the drift tooling as part of other work. Vault documents are under
`projects/agent-queue/specs/` in the operator vault. They are cited by file name, with
the review id and state at the time of writing.

### Open epics and tasks

**`bold-zenith-11`, event-driven hibernation and verified waits** (IN_PROGRESS). It
adds a new config section `wait_policy` (`WaitPolicyConfig`). The section has:
- three feature flags, `v2_enabled`, `hibernation_enabled` and
  `scheduler_waits_enabled`, all false until cutover;
- per-kind `escalate_after` and `expected_duration`;
- `in_place_ceiling_seconds` (1800), `hibernate_margin` (2.0) and
  `resume_cost_minutes` (10);
- a per-project override map.

**`WaitPolicyConfig` is not on `main`.** It exists only on `origin/aq/bold-zenith-11.2`.
When it lands, it must be classified hot or restart and added to the section list. It
also needs a round-trip mapper and a regenerated configuration schema.

| Task | State | What it changes here |
|---|---|---|
| `.22` Flip defaults and cut over | READY | Turns the new flags on, refuses version-1 wait bodies, and adds doctor checks for installed-copy drift, plus the runbook. |

The sources are `2026-10-13-event-driven-agent-hibernation-and-verified-waits.md`
(`rev-bold-pinnacle`, approved, revision 2) and its implementation specification
`…-implementa.md` (`rev-fleet-orbit`, approved, revision 1).

**`stark-willow-95`, fast and smooth integration** (IN_PROGRESS). It implements
`2026-10-12-fast-smooth-integration.md`. The review record `rev-fair-lantern` shows
revision 2 approved. The vault frontmatter's "revision 3, in review" is stale: revision
3 was never submitted.

| Task | State | What it changes here |
|---|---|---|
| `.7` Replace the timed train window | READY | Cadence fields stop having an effect but stay parseable, so existing configs still load. |
| `.10` Make epic-level checks advisory | DEFINED | Adds a per-project opt-in for required checks. |
| `.11` Shared build cache | DEFINED | Adds a `workspaces.build_cache` setting. |
| `.15` Author budget | DEFINED | Adds per-project `author_budget_seconds` and `author_assist`, omitted from dumps at their defaults so that in-flight policy snapshots compare equal. |
| `.16` Speculative CI | DEFINED | Adds an opt-in setting. |
| `.17` Align shipped policy, profiles, instructions and retirement ratchets | DEFINED | Updates shipped profiles, skills and reviewed bundles. Keeps capability sync additive and reports installed drift without overwriting it. Documents `cadence_seconds` and `settling_cap_seconds` as deprecated. |

**`calm-grove-14`, H09** (READY). It updates the shipped templates and aq skills, and
the vault mirror of the Discord-simplification spec. That mirror must stay
byte-identical to the repository copy (AGENTS.md). **`vivid-rapids-36`** (H09-OPS,
READY) is the operator step that verifies it:
- it checks the mirror by SHA-256;
- it runs `profiles.system_drift` and `skills.installed_drift`;
- it merges grants or reseeds.

**`vivid-pinnacle-60`** (PAUSED) is the H08 counterpart. The source for all three is
`2026-10-11-one-place-to-ask-jack-implementation-specification.md` (`rev-sharp-zenith`,
approved, revision 1).

**Held spec ingestion.** Source:
`2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md`
(`rev-agile-beacon`, approved, revision 1). It has three parts:
- `nimble-lantern-37` (IN_PROGRESS) has two child tasks:
  - `.2` (READY) rewrites the spec-ingest profile text and removes the
    `task_batch_commit` grant from the shipped profile. Installed copies keep the
    grant, because capability sync only adds and write-if-absent never rewrites, so an
    operator reseed is part of the rollout.
  - `.3` (DEFINED) updates the docs.
- `calm-orbit-69.3` (DEFINED) covers the CLI, doctor and MCP surfaces.
- `sharp-cascade-53.2` is PAUSED.

**`clear-impact-72.1`–`.11`, playbook building blocks** (READY). Each task evaluates
rewriting one shipped playbook onto reusable building blocks. That changes the
reviewed bundles that seeding refreshes into the vault. Source:
`2026-10-12-implementation-playbooks-reusable-building-blocks.md` (`rev-smart-flare`,
approved, revision 1). `wise-orbit-24` (BLOCKED) is the related playbook refactor F3.

**`agile-vault-38`** (BLOCKED) would make playbook scripts require
`scripts.runtime_root`, a runtime manifest, `input_root` and `state_root`, with
defaults under `~/.agent-queue`. It conflicts with
`2026-10-14-playbook-scripts-no-sandbox-decision.md`, a decision note with no review.
That note records the human's choice to "just allow playbooks to run scripts", so
this task is likely to be dropped.

**`grand-zenith-64`** (PAUSED) ingests the system inventory spec this page belongs to.

### In-flight and recent specs

| Document | Review and state | What it means for this system |
|---|---|---|
| `2026-10-20-agent-queue-system-inventory.md` | `rev-bright-rapids`, approved (revision 1) | Defines this system's scope and its 17 neighbours. |
| `2026-10-13-event-driven-agent-hibernation-and-verified-waits.md` | `rev-bold-pinnacle`, approved (revision 2) | The `wait_policy` section and its per-project map. |
| `2026-10-13-event-driven-agent-hibernation-and-verified-waits-implementa.md` | `rev-fleet-orbit`, approved (revision 1) | The task plan for `bold-zenith-11`, including doctor drift checks. |
| `2026-10-12-fast-smooth-integration.md` | `rev-fair-lantern`, approved (revision 2); frontmatter stale | Deprecates cadence settings, adds per-project and workspace settings, and aligns shipped vault content. |
| `2026-10-11-one-place-to-ask-jack-implementation-specification.md` | `rev-sharp-zenith`, approved (revision 1) | Template and skill updates, the vault mirror and the drift-check runbook. |
| `2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md` | `rev-agile-beacon`, approved (revision 1) | A shipped profile change that installed copies will not pick up without a reseed. |
| `2026-10-12-implementation-playbooks-reusable-building-blocks.md` | `rev-smart-flare`, approved (revision 1) | Changes shipped reviewed bundles. |
| `2026-10-14-playbook-scripts-no-sandbox-decision.md` | None (decision note) | Argues against new `scripts.*` config. |
| `2026-10-01-aq-work-and-knowledge-records-…` | `rev-fleet-cascade`, approved (revision 2) | Makes the vault an export target for knowledge records rather than their source. |
| `2026-10-03-k14-knowledge-records-operator-guide.md` | `rev-solid-ridge`, approved (revision 1) | The operator view of record export into the vault. |

In the repository:
- [2026-09-10-config-api-secret-redaction-design.md](../../superpowers/specs/2026-09-10-config-api-secret-redaction-design.md)
  is the implemented design for redaction;
- [portable-config.md](../portable-config.md) is the current behaviour spec for
  bundles.

### What the October changes established

The month's work set the current shape. Later changes should keep it:
- write-if-absent seeding, with tombstones for removed defaults;
- additive capability sync as the only automatic profile change, plus doctor checks
  that name drift instead of repairing it silently;
- hash-matched harness refresh;
- reviewed bundles as the only playbook content that seeding replaces;
- the vault as an export target for knowledge records;
- secrets as `${VAR}` references, redacted on read and restored on write;
- new settings omitted from dumps at their defaults, so that policy snapshots taken
  before an upgrade still compare equal.

## Source and tests

| Area | Source | Focused tests |
|---|---|---|
| Loading and validation | [src/config.py](../../../src/config.py) | `aq test tests/test_config.py tests/test_config_validation.py tests/test_config_profiles.py` |
| Section mapping round trip | [src/config.py](../../../src/config.py) | `aq test tests/test_config_section_roundtrip.py tests/test_config_roundtrip.py` |
| Hot reload | `ConfigWatcher` in [src/config.py](../../../src/config.py) | `aq test tests/test_config_watcher.py` |
| Editing | [src/config_editor.py](../../../src/config_editor.py), [src/cli/system_config.py](../../../src/cli/system_config.py) | `aq test tests/test_config_editor.py tests/test_cli_system_config.py` |
| Secrets | [src/config_secrets.py](../../../src/config_secrets.py) | `aq test tests/test_config_secrets.py` |
| Tuning | [src/config_tuning.py](../../../src/config_tuning.py) | `aq test tests/test_config_tuning.py` |
| Portable bundles | [src/portable_config.py](../../../src/portable_config.py) | `aq test tests/test_portable_config.py` |
| Generated schema | [scripts/generate-config-schema-inventory.py](../../../scripts/generate-config-schema-inventory.py) | `aq test tests/test_config_schema_inventory.py`; `scripts/regenerate-generated.sh --check` |
| Vault layout, seeding, migration | [src/vault.py](../../../src/vault.py) | `aq test tests/test_vault.py tests/test_vault_manager.py tests/test_vault_migration.py` |
| Harness refresh and drift | [src/sessions/harness_manifest.py](../../../src/sessions/harness_manifest.py), [src/profiles/drift.py](../../../src/profiles/drift.py) | `aq test tests/test_harness_manifest.py tests/test_profile_drift.py` |
| Watcher | [src/vault_watcher.py](../../../src/vault_watcher.py) | `aq test tests/test_vault_watcher.py tests/test_vault_watcher_service.py` |
| Index pages | [src/vault_index.py](../../../src/vault_index.py) | `aq test tests/test_vault_index.py tests/test_vault_index_summaries.py` |
| Facts, overrides, READMEs | [src/facts_handler.py](../../../src/facts_handler.py), [src/facts_parser.py](../../../src/facts_parser.py), [src/override_handler.py](../../../src/override_handler.py), [src/readme_handler.py](../../../src/readme_handler.py) | `aq test tests/test_facts_handler.py tests/test_facts_parser.py tests/test_facts_bidirectional_sync.py tests/test_override_handler.py tests/test_override_injection.py tests/test_readme_handler.py` |
| Reference stubs | [src/reference_stub_enricher.py](../../../src/reference_stub_enricher.py) | `aq test tests/test_reference_stub_enricher.py tests/test_reference_stub_regeneration.py` |
| Prompts and templates | [src/prompt_builder.py](../../../src/prompt_builder.py), [src/prompt_manager.py](../../../src/prompt_manager.py), [src/task_templates.py](../../../src/task_templates.py) | `aq test tests/test_prompt_builder.py tests/test_prompt_manager.py tests/test_prompt_task_templates.py` |

Tests for inert or dead code still pass. They test the modules in isolation:
- `tests/test_vault_glossary.py`;
- the facts and override suites;
- `tests/test_prompt_builder.py`.

A green run there says nothing about whether the path is wired into the daemon.

When changing this system:
- **Adding a config section:** add the dataclass and choose hot or restart. Use
  `_dataclass_kwargs` rather than a hand-written mapper. Then run
  `scripts/regenerate-generated.sh` and commit the regenerated schema.
- **Editing shipped vault content** (a profile, skill, class or harness): it reaches
  existing installs only through the refresh paths above, `aq agent profile-reseed`
  or `aq vault reset-harness`. Name the operator step in the task.

Related reading, in order:
1. [configuration-and-vault.md](../../concepts/configuration-and-vault.md) for the
   concepts;
2. [configuration.md](../../reference/configuration.md) for every key, keeping the
   reload-table correction above in mind;
3. [default-tuning.md](../../guides/default-tuning.md) for tuning;
4. [portable-config.md](../portable-config.md) for bundles;
5. [docs/reference/modules/vault.md](../../reference/modules/vault.md) for the folder
   map;
6. [profiles.md](../design/profiles.md) and [worker-pools.md](../../guides/worker-pools.md)
   for what the seeded profiles and classes become;
7. [memory-plugin.md](../design/memory-plugin.md) and
   [feature-pauses.md](../design/feature-pauses.md) for why the facts and override
   paths are idle.
