# Configuration, the vault, and prompt context

AQ keeps operational settings, human-authored knowledge, and durable runtime records
in different places. Changing a policy should not mean editing a database row or
rebuilding an agent prompt by hand.

## Why it exists

An AQ installation needs settings before it can start, such as a PostgreSQL connection
and a directory for workspaces. It also needs material people revise over time: agent
profiles, playbooks, project notes, facts, and prompt guidance. The first belongs in
YAML configuration; the second belongs in the Markdown vault; the database stores
durable coordination records and projections.

## Vocabulary

* **Configuration** is config.yaml plus its overlays: typed runtime settings loaded
  by [src/config.py](../../src/config.py).
* **Vault** is the Obsidian-compatible Markdown tree at
  ~/.agent-queue/vault/ by default. It is where users author policy and knowledge.
* **Projection** is a database or search-index representation derived from a vault
  file; it accelerates runtime work but is not the editable source.
* **Fact** is a small namespaced key/value entry in a facts.md file.
* **Prompt context** is the ordered text and tool schema given to an LLM call.

## A realistic example

Assume the daemon is running, a project named demo exists, and its worker needs to
know the test command. Put the stable, reviewed fact in the project vault:

~~~text
~/.agent-queue/vault/projects/demo/facts.md

## Project
- language: Python 3.12
- test_command: aq test tests/test_config.py
~~~

The parser accepts headings as namespaces and the first colon as the key/value divider.
This is the same small input/output transformation AQ uses:

~~~bash
python3 - <<'PY'
from src.facts_parser import parse_facts_file, render_facts_file
facts = parse_facts_file('## Project\n- language: Python 3.12\n- test_command: aq test tests/test_config.py\n')
print(facts)
print(render_facts_file(facts), end='')
PY
~~~

~~~text
{'Project': {'language': 'Python 3.12', 'test_command': 'aq test tests/test_config.py'}}
## Project
language: Python 3.12
test_command: aq test tests/test_config.py
~~~

With the optional memory service connected, a changed facts file is copied into that
service's scoped KV store. Without it, the in-tree handler logs the change and leaves
the Markdown file intact. See [src/facts_handler.py](../../src/facts_handler.py).

## Inputs and outputs

### Configuration inputs

The default configuration location is ~/.agent-queue/config.yaml. Its adjacent .env
file supplies secret values referenced as ${NAME}; it does not overwrite an environment
variable already set. AQ then reads base YAML, an optional environment overlay, and an
optional named profile. The detailed order is in the
[configuration reference](../reference/configuration.md).

~~~yaml
data_dir: ~/.agent-queue
workspace_dir: ~/agent-queue-workspaces
database:
  url: ${AQ_DATABASE_URL}
messaging_platform: none
sessions:
  enabled: true
~~~

database.url must resolve to a PostgreSQL DSN. workspace_dir must be writable (or have
a writable parent). These are startup-required settings; changing them in a file does
not move a running daemon. [src/config.py](../../src/config.py) validates known
problems together rather than silently selecting a fallback.

### Vault inputs

data_dir determines the vault root. Startup creation is idempotent: existing user files
are kept, while missing shipped templates and defaults are copied in.

| Purpose | Default vault path |
|---|---|
| System playbooks | vault/system/playbooks/*.md |
| Global agent profiles | vault/agent-types/&lt;profile&gt;/profile.md |
| Profile knowledge and facts | vault/agent-types/&lt;profile&gt;/memory/, facts.md |
| Project knowledge, facts, references, and overrides | vault/projects/&lt;project&gt;/… |
| Project-specific prompt override | vault/projects/&lt;project&gt;/overrides/&lt;profile&gt;.md |
| Starter templates and harness descriptions | vault/templates/, vault/harnesses/*.md |

The practical output is Markdown editable with a normal editor or Obsidian. The vault
watcher notices creations, edits, and deletions after its initial snapshot; each owning
handler determines whether the edit also produces a projection or summary.

### Prompt output

PromptBuilder.build() returns one system-prompt string and a list of JSON Schema tool
definitions. Its implemented order is:

~~~mermaid
flowchart TD
  P[profile.md: Role / Rules / Reflection] --> L0[L0 role]
  O[project override Markdown] --> OV[override]
  F[facts.md] --> L1F[L1 facts]
  G[memory/guidance Markdown] --> L1G[L1 guidance]
  M[optional memory search] --> L2[L2 context]
  T[src/prompts/*.md] --> I[identity template]
  C[caller task/project blocks] --> CB[context blocks]
  L0 --> A[ordered prompt]
  OV --> A
  L1F --> A
  L1G --> A
  L2 --> A
  I --> A
  CB --> A
  Tools[tool JSON schemas] --> OUT[PromptBuilder.build output]
  A --> OUT
~~~

The override follows the profile role and is therefore more specific project guidance.
Empty pieces are omitted. Template variables use {{name}}; an unknown variable stays
visible. Large tier text produces a warning at twice its approximate budget, not a
silent truncation. See [src/prompt_builder.py](../../src/prompt_builder.py).

## State ownership

| State | Canonical owner | Derived/runtime consumers |
|---|---|---|
| Runtime settings and overlay selection | YAML and .env beside config.yaml | typed AppConfig, config schema/editor |
| Profiles, playbooks, knowledge, facts, overrides, harnesses | Vault Markdown | watcher handlers, registries, prompt assembly |
| Tasks, claims, attempts, integrations | PostgreSQL | dashboard, CLI, orchestrator |
| Semantic chunks, scoped KV, search results | Optional external aq-memory plugin | L1/L2 prompt inputs and vault projections |
| Vault hubs and README summaries | Generated Markdown under the vault | Obsidian navigation and supervisor context |

In-tree code supports file formats, deterministic fact parsing, vault watching, and
prompt assembly without a memory plugin. Semantic retrieval, embeddings, and scoped KV
are optional plugin capabilities. Keep the editable vault file as the policy source.

## Common failures and recovery

| Symptom | Cause and recovery |
|---|---|
| Daemon rejects configuration | Correct every listed validation error, especially database.url or a missing environment variable, then restart. aq system config schema is an operator-facing field/type inspection command. |
| A changed setting seems ignored | Run aq system reload-config; restart for sections reported as restart-required. See the [reload reference](../reference/configuration.md#reload-and-restart). |
| A vault edit has no immediate effect | The watcher polls and debounces. Check the vault root and handler path pattern, save again, and allow a polling/debounce cycle. |
| Facts are absent from semantic retrieval | The external memory plugin may be unavailable. Markdown remains canonical; restore/configure the plugin before expecting KV/search projection. Deleted fact files deliberately retain old KV entries until explicit removal. |
| An override has no effect | Check projects/&lt;project&gt;/overrides/&lt;profile&gt;.md and the selected profile. Malformed override paths are rejected by the indexer. |
| A reference stub is stale or unenriched | Confirm its source still exists, then use the operator vault workflow to rebuild/re-enrich it. Missing-source and unchanged-but-unenriched states are distinct. |

## How it works

[src/vault.py](../../src/vault.py) creates, migrates, and seeds vault content.
[src/vault_manager.py](../../src/vault_manager.py) resolves paths without side effects;
its explicit ensure_* and register_* methods create directories.
[src/vault_index.py](../../src/vault_index.py) makes Obsidian hub files, and
[src/wiki_links.py](../../src/wiki_links.py) parses and resolves their wiki links.

[src/vault_watcher.py](../../src/vault_watcher.py) uses portable mtime/size polling,
batches matching paths, and flushes after a debounce period (or maximum pending age).
It differs from [src/file_watcher.py](../../src/file_watcher.py), which watches
configured workspace files/folders and emits file.changed or folder.changed events.

Facts, overrides, and project READMEs are handler families. Facts parse deterministically;
overrides can be embedded into project memory when the plugin is installed; a project
README produces a deterministic supervisor summary. Reference stubs may be enriched by
a direct LLM call, guarded by a source hash to skip unchanged content. See
[src/readme_handler.py](../../src/readme_handler.py) and
[src/reference_stub_enricher.py](../../src/reference_stub_enricher.py).

> **Shipped defaults.** In-tree prompt templates, default playbooks, intelligence
> classes, profiles, harnesses, and vault templates seed only a missing local copy.

> **Configured local policy.** data_dir, environment/profile overlays, and local vault
> content are installation choices.

> **Optional compatibility.** database_path is a deprecated alias for database.url;
> PostgreSQL is required. Legacy Discord per-project-channel YAML is accepted only to
> warn during migration and cannot restore old channel creation.

> **Proposed work.** This page documents current source behavior. Historical material
> under docs/specs/ may describe proposals or retired paths, not operating defaults.

## Related pages

* [Configuration reference](../reference/configuration.md) — precedence and reload.
* [Profiles and intelligence classes](../reference/profiles-and-classes.md) — profile
  Markdown that supplies role content.
* [Agents and routing](agents-and-routing.md) — profile and harness selection.
* [Database data lifecycle](../reference/database/data-lifecycle.md) — durable records
  that are database state rather than vault content.

## Source and tests

The complete inventory is the [vault module catalog](../reference/modules/vault.md).
Focused checks: aq test tests/test_config.py tests/test_config_watcher.py,
aq test tests/test_vault_watcher.py tests/test_facts_handler.py, and
aq test tests/test_prompt_builder.py tests/test_prompt_manager.py.

