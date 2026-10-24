# System spec: configuration and vault

Where an Agent Queue install keeps its settings and its markdown, and how both reach a
running daemon. This page covers:
- the operator config file `~/.agent-queue/config.yaml`: how it is loaded, validated,
  edited, reloaded, tuned, redacted and made portable;
- secrets, preferably `${VAR}` references resolved from the environment or a `.env` file;
- the vault `~/.agent-queue/vault/`: its layout, the write-if-absent seeding of
  shipped content, the watcher that reacts to edits, and the index pages;
- the vault side-channels: facts files, project overrides, project READMEs and
  reference stubs;
- the prompt-building code that is not `aq prime`: the legacy `PromptBuilder`, the
  per-workspace prompt manager and the task templates.

This is system 13 of the 18 in the system inventory. It describes `origin/dev` at `52d2efe4c` as of
2026-10-22. Where a guide already explains something well, this page gives the short
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
- [Recent shipped changes](#recent-shipped-changes)
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
- **Secret-safe configuration reads.** Prefer `${VAR}` references. Literal credentials
  are also accepted; the config API redacts them on read and restores placeholders
  on write. References themselves remain visible.
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

There are no separately loaded per-project config files. The installation config
has project/repository maps, including `project_roots`,
`resources.test_interpreters`, per-repository `worktrees` timeouts,
`workspaces.projects` cache overrides and `wait_policy.projects` policy overrides.
Other per-project settings live in database policy columns or the vault.

YAML agent defaults use `agents`, while the typed attribute is `agents_config`.
The schema/tuner/portable exporter currently use the typed name as a YAML key;
the loader ignores it. See [Behaviour gaps](#behaviour-gaps).

### Hot reload and restart

[src/config.py](../../../src/config.py) declares two disjoint sets that together cover
all 60 sections:

- **Hot (33)** (`HOT_RELOADABLE_SECTIONS`, about line 4320). These are:
  - `scheduling`, `monitoring`, `archive`, `llm_logging`, `agents_config`,
    `auto_task`, `logging`;
  - `agent_profiles`, `global_token_budget_daily`, `max_daily_playbook_tokens`,
    `max_concurrent_playbook_runs`;
  - `rate_limits`, `state_machine`, `work_graph`, `swarm`, `resources`,
    `test_selection`, `metrics`;
  - `providers`, `provider_failover`, `routing`, `docs`, `graph_layout`,
    `dashboard_server`, `host_shell`, `pricing`;
  - `surface`, `project_roots`, `git_identity`, `deploy`, `knowledge`,
    `workspaces`, `wait_policy`.
- **Restart required (27)** (`RESTART_REQUIRED_SECTIONS`, about line 4373). These are:
  - `database`, `database_path`, `data_dir`, `workspace_dir`, `env`, `profile`;
  - `discord`, `messaging_platform`, `mcp_server`, `api_auth`, `security`;
  - `llm`, `memory`, `memory_extractor`, `messages`, `inbox`, `events`,
    `streams`, `validate_events`;
  - `sessions`, `supervisor`, `supervisor_agent`, `worktrees`, `integration`,
    `playbooks`, `health_check`, `reports`.

`ConfigWatcher` (about line 4441) polls the file's mtime every
`scheduling.config_poll_interval_seconds` (default 30). It watches only the base
file’s mtime; overlay or `.env` edits alone need a forced reload. On a change it reloads,
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

The section writers use [src/config_editor.py](../../../src/config_editor.py), which
uses ruamel in round-trip mode. Comments, order and quotes outside a replaced
section survive; replacing a section can lose its internal comments. Its parts:
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
| `update_config` | 393 | Restores redacted secrets, preflights a temporary candidate, then writes one section. A temporary backup restores only write exceptions and is removed before live reload. |
| `reload_config` | 522 | Re-reads the file now. |
| `preview_portable_config` / `export_portable_config` / `import_portable_config` | 325 / 340 / 360 | See [Portable bundles](#portable-bundles). |

The CLI is `aq system config` ([src/cli/system_config.py](../../../src/cli/system_config.py)):
- `get [SECTION]`;
- `set KEY=VALUE [--dry-run]`;
- `edit` (opens `$EDITOR` on redacted whole-config YAML, then submits changed sections);
- `schema`;
- `tune`;
- `git-identity`.

The dashboard's settings page uses the same commands through the API.

### Secrets

There is no keyring. [src/config_secrets.py](../../../src/config_secrets.py) handles
both ends of the secret path:
- **Storage:** prefer the environment or `~/.agent-queue/.env` with `${VAR}` in YAML.
  Literal YAML credentials are accepted too; there is no storage-enforcement rule.
- **Redaction on read:** `redact_config` replaces values with `__aq_redacted__`. It
  redacts a value when either test matches:
  - the key name matches a secret pattern (`token`, `password`, `secret`, `api_key`
    and so on);
  - the value looks like a credential: `sk-…`, `ghp_…`, `xox…`, `AKIA…`, `AIza…`,
    PEM blocks, or URLs with embedded credentials.
- **Restore on write:** `restore_section_secrets` puts the original values back
  wherever the incoming section still holds the placeholder. A client can therefore
  round-trip a section without seeing literal secrets. Unknown placeholders are
  refused. Lists match by position, so reordering redacted entries is unsafe.
  Credential-bearing URLs preserve the URL structure and redact the password.

The design is
[2026-09-10-config-api-secret-redaction-design.md](../../superpowers/specs/2026-09-10-config-api-secret-redaction-design.md).

### Machine tuning

[src/config_tuning.py](../../../src/config_tuning.py) recommends settings from the
hardware. The steps are:

1. `MachineResources` measures CPU count and RAM, not disk.
2. `recommended_tuning` derives agent capacity, test slots, resource gates and
   background/report cadence, plus curated policy defaults. It does not derive DB pools.
3. `DERIVED_KEYS` lists values deliberately omitted so runtime derivation stays active.
4. `tuning_plan` classifies whole sections as add, keep, replace or unchanged.
5. `apply_tuning` preflights a candidate, refuses newly introduced validation errors,
   reports pre-existing errors separately, then writes the selected sections locally.
   It does not force live reload or retain a transaction backup.

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
  existing value, replace it, or raise an error on conflict. Import `dry_run` validates
  without writes. `bundle_preview` instead previews an export. `wait_policy`, including
  project overrides, travels; local `workspaces` and Git identities do not.

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
3. **Copy templates**, including task templates from `src/prompts/task_templates/`
   (about line 1417).
4. **Seed harnesses** from `src/sessions/default_harnesses/` (about line 1657).
5. **Seed formulas** from `src/prompts/formulas/` (about line 1607).
6. **Seed intelligence classes** from `src/prompts/default_intelligence_classes/`
   (about line 1625). A `.retired` marker stops a class the operator removed from
   coming back.
7. **Seed profiles** (`ensure_default_profiles`). A `.retired-defaults` tombstone
   records shipped ids the operator removed.
8. **Seed aq skills** (`ensure_default_aq_skills`, about line 1686). These go to
   `~/.claude/skills`, `~/.gemini/skills` and `~/.codex/skills`, not to the vault.
9. **Regenerate the hub index pages** (`VaultIndexGenerator.generate_all()`).

Authored profile, class, skill and template defaults use **write-if-absent**, so a
fix to a shipped profile or skill does not replace an installed copy. Hub pages are
generated output. Three narrow paths can refresh other installed content:

- **Harnesses** are refreshed when the installed file's hash matches an *older shipped*
  version ([src/sessions/harness_manifest.py](../../../src/sessions/harness_manifest.py)).
  An operator-edited harness is left alone. `aq vault reset-harness` replaces it on
  request.
- **Reviewed playbook bundles** (`ensure_reviewed_playbook_bundles`,
  [src/playbooks/required.py](../../../src/playbooks/required.py) about line 210) are
  replaced during initial installation or explicit repair, with `.bak-<epoch>`
  backups. Restarts do not refresh bundles, resurrect deleted policy or repoint
  durable activations. First install copies only `INITIAL_SYSTEM_PLAYBOOK_IDS`, not
  every shipped bundle. An existing non-empty vault gets only the receipt.
  Initial activation covers the three required policies plus `supervisor-failure-triage`
  and `default-pipeline`; `morning-report` also needs explicit activation and report
  enablement. `blocked-task-escalation` is not a default activation.
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
| `workspace.spec.changed` event | 2496 | [src/reference_stub_enricher.py](../../../src/reference_stub_enricher.py) | Fills a stub's summary with a direct model call. It caps source text at the configured limit (default 20,000 characters) and skips an already-enriched stub whose `source_hash` is unchanged. Also gated on `stub_enrichment_enabled`. |

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
  `_build_task_context_with_prompt_builder` lost its last caller in commit 533bda271
  (2026-09-01) and was removed with the retired integration adapters (azure-ridge-72).
- **The prompt manager** ([src/prompt_manager.py](../../../src/prompt_manager.py))
  serves per-workspace templates from `<workspace>/prompts/`. It supports optional
  strict variables. Only the `list_prompts`, `read_prompt` and `render_prompt`
  commands use it (system_commands.py about lines 1286-1414).
- **Task templates** ([src/task_templates.py](../../../src/task_templates.py)) render
  shipped task bodies with strict variables. The vault copy under
  `templates/tasks/` wins over the packaged one.

Bootstrap prompts are constants in
[src/sessions/spec.py](../../../src/sessions/spec.py) (`BOOTSTRAP_PROMPT`,
`POOL_BOOTSTRAP_PROMPT`, plus `NAMED_BOOTSTRAP_PROMPT`). These format launch-time
values. Prime reads profile `Role` and `Rules`; machine-only headings such as
`Reflection` are not rendered as role text. Generated task bodies use the live
[task-template workflow](../../guides/generated-task-templates.md).

What in `src/prompts/` is live:

| Path | Status |
|---|---|
| `default_intelligence_classes/` (7), `formulas/` (2), `task_templates/` (72) | Seeded into the vault, write-if-absent. |
| `reviewed_playbooks/` | Initial selected bundles or explicit import/repair only (owned by playbooks, system 8). |
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
2. **Secret reads are redacted.** `${VAR}` references are recommended and stay visible.
   Literal credentials are accepted, redacted on API reads, and restored on writes
   carrying the retained placeholder.
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
   - reviewed bundles during initial installation or explicit repair, with a backup;
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

### Documentation status

The October 22 audit corrected `.env` precedence, the full hot/restart classification,
retired `pause_retry`, actual CLI syntax, temporary-backup behavior, tuning preflight,
portable import/export preview, first-install policy installation and inactive legacy
memory paths in the operating guides. The old
[vault design](../design/vault.md) and [prompt-builder proposal](../prompt-builder.md)
are explicitly historical; they do not define the current runtime.

### Stale code and comments

These remain source issues; generated documentation must be fixed through its sources:

- `config_editor.SECTION_NOTES` still describes some shipped lanes as disabled or
  unfinished. The generated schema inherits those notes; editing the JSON alone
  would be overwritten by generation.
- `VaultWatcher` describes a standalone background loop, while the orchestrator tick
  drives `check()`. For patterns without `**`, its `fnmatch` matcher lets `*` cross
  `/`, contrary to the docstring; `**` patterns use segment matching.
- `prompt_manager.py` cites the nonexistent `specs/prompt-system.md`.
- Legacy seeders, `FileWatcher`, glossary/wiki-link helpers and the layered
  `PromptBuilder.build()` retain isolated coverage without active daemon callers.
  Their unit tests do not prove that an edit reaches a worker session.

### Behaviour gaps

These are code follow-ups, not behavior promised by the operating docs:

- **Agent-default key mismatch.** `load_config` reads YAML `agents`, but schema,
  tuning and portability use `agents_config`. A local probe with
  `stuck_timeout_seconds: 1901` loaded 1901 under `agents` and the default 0 under
  `agents_config`. Tuning can therefore report a recommendation written without
  making it effective. Section classification also uses the typed name.
- **Overlay preflight is incomplete.** `validate_candidate_config` and
  `update_config` load a temporary sibling filename, whose stem misses the real
  environment overlay, and pass no caller-selected profile. A candidate can pass
  preflight but fail when loaded through the real path. `planner` and `plugins` also
  remain on the portable allowlist although `load_config` ignores them.
- **Unknown update sections are accepted.** `update_config` does not check the
  section name; a typo can persist as an ignored top-level key.
- **Writes are not atomic.** `write_section` opens with `"w"`. `update_config`
  removes its backup after the write and does not roll back a later reload failure.
  `tune --apply` validates locally but bypasses `CommandHandler`, command events and
  a multi-section backup transaction.
- **Redacted list entries restore by position.** Reordering a list can associate a
  retained secret with the wrong item. Unresolved placeholders are rejected, but
  position changes are not identity matching.
- **Legacy facts and override ingestion is inactive.** The plugin loader always
  skips `LEGACY_MEMORY_PLUGINS = {"aq-memory", "memory"}`. Facts handlers only log;
  `OverrideIndexer` is not attached. Legacy L1/L2 builders return empty text.
  Authorized knowledge-record bundles can still populate prime context; this is
  separate from the retired retrieval path. Historically, facts deletion also left
  KV entries behind.
- **Hub detection is filename-based.** `<dir>/<dir>.md` is treated as a hub. A real
  note with that name can be overwritten or removed by generation. Some definition
  loaders filter generated hubs, so this is not a blanket failure of every loader.
- **Hot sections depend on live readers.** Classification updates `AppConfig`;
  consumers retaining a copied sub-config need their own reload handling. Base-file
  polling also does not detect an overlay or `.env` edit by itself.

## Recent shipped changes

This records source history, not live task statuses. The previous October 20 queue
snapshot is obsolete; inspect the queue through authorized operator surfaces when
planning work. Configuration and vault changes in the six-week audit window include:

| Date | Source commit | Current behavior |
|---|---|---|
| October 22 | `597b44ff5` | Integration service reconciliation gains bounded phantom-job policy. |
| October 21 | `56bde6dad` | `workspaces` adds warm integration worktrees and per-project compiler-cache configuration; see the [reference](../../reference/configuration.md#warm-integration-checkouts-and-compiler-caches). |
| October 21 | `53d161b35` | Playbook script runner operates by default; old sandbox terminology and disabled-foundation descriptions are historical. |
| October 21 | `08a75ae4d` | `wait_policy.resume_stall_seconds` bounds stalled resumption, globally and per project. |
| October 20 | `a278d71d3`, `097df3f96` | Verified-wait rollout flags default on, v1 bodies default to refusal, and wait policy survives portable round trips. |
| October 19 | `4168e6312`, `09f7c46e9` | Wait metrics accept a historical cohort; live prime context budgets derive from model windows rather than the legacy builder's fixed warning tiers. |
| October 13 | `f50f3c882`, `b7fe4362c` | Project wait-policy overrides ship; system playbook sources can be recovered from active reviewed artifacts. |
| October 12 | `6144e8419`, `f56b6a92c` | Portable bundles carry full typed setup and policy dependencies with local-field exclusions. |
| October 12 | `f64b31c5e` | Generated task instructions move into strict vault-backed task templates; installed copies remain write-if-absent. |
| October 11 | `3b6fb9578`, `1c0400b35` | Playbooks install deliberately once; restart does not resurrect retired system policy. |
| October 6 | `189e0ac21`, `5c8c1f13c` | Release-tag deployment and remote-dashboard host-shell policy are configurable. |
| October 3 | `7db6bf070` | Authorized database knowledge bundles supply context; vault knowledge files are exports. |
| October 1 | `d14df8e5d` | Git commit identity is centrally configurable. |

The current field inventory is generated from the dataclasses. Use
`aq system config schema` and [configuration-schema.json](../../reference/configuration-schema.json)
for exact fields and bounds; regeneration does not make stale source notes authoritative.

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
2. [configuration.md](../../reference/configuration.md) for keys and reload behavior;
3. [default-tuning.md](../../guides/default-tuning.md) for tuning;
4. [portable-config.md](../portable-config.md) for bundles;
5. [docs/reference/modules/vault.md](../../reference/modules/vault.md) for the folder
   map;
6. [profiles.md](../design/profiles.md) and [worker-pools.md](../../guides/worker-pools.md)
   for what the seeded profiles and classes become;
7. [memory-plugin.md](../design/memory-plugin.md) and
   [feature-pauses.md](../design/feature-pauses.md) for why the facts and override
   paths are idle.
