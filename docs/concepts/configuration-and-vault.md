# Configuration, the vault, and prompt context

AQ keeps runtime settings in YAML, authored definitions in a Markdown vault, and
durable coordination and knowledge records in PostgreSQL. Editing a file affects
runtime behavior only when its owning loader or watcher consumes it.

## Vocabulary

* **Configuration** is `config.yaml` plus environment/profile overlays, loaded into
  typed settings by [src/config.py](../../src/config.py).
* **Vault** is the Obsidian-compatible tree at `~/.agent-queue/vault/` by default.
  Profiles, classes, harnesses, formulas and other authored definitions live here.
* **Knowledge record** is database-owned content exposed through AQ knowledge
  commands. Its Markdown export is a projection; editing that export does not update
  the record.
* **Fact** is a namespaced key/value entry in a legacy `facts.md` file. Parsing still
  works, but the daemon does not connect its legacy KV/indexing service.
* **Prompt context** is the role, rules, task and authorized context delivered to a
  session through `aq prime`.

## Configuration inputs

The default configuration location is `~/.agent-queue/config.yaml`. Its adjacent
`.env` supplies `${NAME}` values and **overrides** an already-set process variable,
with a warning when values differ. AQ reads base YAML, merges an optional environment
then profile overlay, substitutes variables, constructs typed settings and validates.
Mappings merge recursively, lists/scalars replace, and overlay `null` removes a key.
See the [configuration reference](../reference/configuration.md) for selection order,
field defaults and reload classification.

```yaml
data_dir: ~/.agent-queue
workspace_dir: ~/agent-queue-workspaces
database:
  url: ${AQ_DATABASE_URL}
messaging_platform: none
sessions:
  enabled: true
```

`database.url` must resolve to a PostgreSQL DSN. `workspace_dir` must be writable or
have a writable parent. Changing these settings does not move a running daemon.
Use `${VAR}` references for credentials; literal credentials are accepted but redacted
on configuration API reads.

The watcher notices changes to the base file's mtime. Overlay or `.env` edits alone
need `aq system reload-config`. Invalid loads retain the current config; restart-only
changes are reported without replacing those live sections.

## Vault inputs

`data_dir` determines the vault root. Startup creates missing directories and seeds
shipped definitions. Existing profiles, classes, skills and templates are kept;
retirement markers prevent intentionally removed profile/class defaults returning.

| Purpose | Default vault path |
|---|---|
| Installed system playbook sources | `vault/system/playbooks/*.md` |
| Global profiles | `vault/agent-types/<profile>/profile.md` |
| Legacy profile memory and facts | `vault/agent-types/<profile>/memory/`, `facts.md` |
| Project notes, specs, references and knowledge exports | `vault/projects/<project>/…` |
| Project prompt override files | `vault/projects/<project>/overrides/<profile>.md` |
| Templates and harnesses | `vault/templates/`, `vault/harnesses/*.md` |
| Classes, formulas and reviewed bundles | `vault/intelligence-classes/`, `formulas/`, `reviewed-playbooks/` |

An absent or empty vault gets initial reviewed playbook installation once, recorded
in `data_dir/playbook-installation.json`. An existing non-empty vault gets a receipt
without policy installation or activation. Restart does not restore deleted policy
or replace active artifacts; import/activation and operator doctor repair are explicit.

Three refresh paths differ from ordinary write-if-absent seeding:

* Harnesses matching an older shipped hash refresh automatically; custom edits stay.
  `aq vault reset-harness` explicitly replaces selected harnesses.
* Supervisor and worker template capability grants merge additively on startup and
  profile reload, unless `capability_sync: false` opts out.
* Reviewed bundles can be replaced during initial installation or explicit repair,
  with backups. They are not refreshed on every restart.

`aq doctor --check profiles.system_drift` and `skills.installed_drift` identify
installed content that differs from shipped content. `aq agent profile-reseed
--profile-id <id> --grants-only` merges grants without replacing operator text.

## What edits reach runtime

The vault watcher takes an initial snapshot, then polls mtime/size and debounces
changes. Profiles, harnesses, classes, formulas, MCP definitions and workspace kinds
have live handlers. Project README changes produce deterministic supervisor Markdown.
Hub pages provide Obsidian navigation; `aq system vault-rebuild-index` rebuilds them,
and `--with-summaries` uses the direct LLM path for folder summaries.

Facts parsing remains available in [src/facts_parser.py](../../src/facts_parser.py):

```python
from src.facts_parser import parse_facts_file

parse_facts_file('## Project\n- language: Python 3.12\n')
# {'Project': {'language': 'Python 3.12'}}
```

This parser example does not publish context to an agent. The daemon skips the legacy
`aq-memory` and `memory` plugins regardless of `memory.enabled`. Facts handlers only
log, and no override indexer is attached. Enabling that flag does not restore facts
or override ingestion. Use current knowledge-record commands for durable context.

Workspace spec watching is separately gated by `memory.enabled` and
`spec_watcher_enabled`. When enabled, it creates reference stubs. Optional enrichment
listens to `workspace.spec.changed`, reads bounded source text and uses a source hash
so an already-enriched unchanged source can be skipped. It also requires
`stub_enrichment_enabled` and a usable direct LLM configuration.

## Prompt output

Worker sessions obtain live role, rules, task and authorized knowledge-record context
through `aq prime`. Legacy L1 facts and L2 retrieval builders return empty text;
authorized knowledge bundles can still populate the rendered context section.

[src/prompt_builder.py](../../src/prompt_builder.py) retains an older assembler and
isolated tests. Its `build()` orders role, project override, facts, guidance, retrieval
context, identity template, caller blocks and tools. It uses lenient `{{name}}`
substitution and warns at twice approximate tier budgets. That build path has no
production caller; prime uses its `extract_section` helper.

[src/prompt_manager.py](../../src/prompt_manager.py) serves workspace `prompts/`
templates through list/read/render commands, with optional strict substitution.
Task templates use strict variables and prefer a vault copy under `templates/tasks/`
over the packaged copy. These are separate from session bootstrap prompts.

## Common failures and recovery

| Symptom | Cause and recovery |
|---|---|
| Configuration is rejected | Correct all listed validation errors, including missing `${VAR}` references and the PostgreSQL DSN. Inspect fields with `aq system config schema`. |
| A setting seems ignored | Force reload after overlay edits; restart for sections classified as restart-required. YAML agent defaults currently use `agents`, while schema/tuning emit `agents_config`; see the [reference](../reference/configuration.md#top-level-sections). |
| A profile edit seems ignored | Check the actual `data_dir`, profile identity and watcher path. Allow a polling/debounce cycle. |
| Facts or an override have no effect | Legacy ingestion is inactive. Changing `memory.enabled` alone cannot enable it. |
| A knowledge export edit has no effect | Update the database record through knowledge commands; Markdown is its export. |
| A shipped fix is absent locally | Inspect drift and use the applicable explicit reseed/reset/repair command. Restart preserves customized definitions. |

## Source and tests

[src/vault.py](../../src/vault.py) creates, migrates and seeds vault content.
[src/vault_manager.py](../../src/vault_manager.py) resolves paths and explicitly creates
layout. [src/vault_watcher.py](../../src/vault_watcher.py) dispatches changes from the
orchestrator tick; its standalone background loop is unused. `FileWatcher`, glossary
and wiki-link utilities retain isolated coverage but do not provide a live generic
workspace watch service.

See the [module catalog](../reference/modules/vault.md) and
[system specification](../specs/systems/configuration-and-vault.md) for source ownership
and focused tests. Related operating guides cover
[profiles and classes](../reference/profiles-and-classes.md),
[worker pools](../guides/worker-pools.md), and
[portable setup](../specs/portable-config.md).
