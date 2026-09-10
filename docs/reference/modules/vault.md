# Module catalog: configuration, vault, and prompt context

These modules are covered by [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md)
and [Configuration reference](../configuration.md). Paths are the source of truth;
the test names identify focused coverage rather than a promise of exhaustive tests.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/config.py](../../../src/config.py) | Loads, overlays, validates, diffs, and watches typed application configuration. | [Configuration reference](../configuration.md) | tests/test_config.py, tests/test_config_watcher.py |
| [src/config_editor.py](../../../src/config_editor.py) | Reads raw YAML, exposes the config schema, and round-trips edits. | [Configuration reference](../configuration.md) | tests/test_config_editor.py, tests/test_config_roundtrip.py |
| [src/config_tuning.py](../../../src/config_tuning.py) | Derives a resource-aware tuning recommendation, and the note explaining each value, from the installing machine. | [Default tuning](../../guides/default-tuning.md) | tests/test_config_tuning.py |
| [src/portable_config.py](../../../src/portable_config.py) | Packs and unpacks an `.aqbundle`: the allowlisted config sections plus each global profile's `profile.md`. | [Default tuning](../../guides/default-tuning.md#relationship-to-portable-bundles) | tests/test_portable_config.py |
| [src/setup_wizard.py](../../../src/setup_wizard.py) | Collects first-run settings and writes config plus protected environment values. | [Configuration reference](../configuration.md) | tests/test_setup_wizard.py |
| [src/vault.py](../../../src/vault.py) | Creates, migrates, seeds, and inspects vault content. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_vault_index.py; profile seed tests |
| [src/vault_manager.py](../../../src/vault_manager.py) | Resolves scoped vault paths and explicitly creates vault directories. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_vault_manager.py |
| [src/vault_watcher.py](../../../src/vault_watcher.py) | Polls and debounces vault changes before path-pattern handler dispatch. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_vault_watcher.py, tests/test_vault_watcher_service.py |
| [src/vault_index.py](../../../src/vault_index.py) | Generates Obsidian hub Markdown and optional folder summaries. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_vault_index.py, tests/test_vault_index_summaries.py |
| [src/vault_glossary.py](../../../src/vault_glossary.py) | Loads glossary concepts and aliases from vault Markdown. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | vault/glossary unit coverage |
| [src/wiki_links.py](../../../src/wiki_links.py) | Parses, resolves, creates, and annotates Obsidian-style wiki links. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_wiki_links.py |
| [src/file_watcher.py](../../../src/file_watcher.py) | Watches configured workspace files/folders and emits filesystem events. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_file_watcher.py |
| [src/facts_parser.py](../../../src/facts_parser.py) | Parses, renders, and diffs namespaced Markdown fact entries. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_facts_parser.py |
| [src/facts_handler.py](../../../src/facts_handler.py) | Maps changed facts files to scopes and syncs them to optional KV storage. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_facts_handler.py, tests/test_facts_bidirectional_sync.py |
| [src/override_handler.py](../../../src/override_handler.py) | Watches and indexes project/profile prompt overrides when memory support is installed. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_override_handler.py, tests/test_override_injection.py |
| [src/readme_handler.py](../../../src/readme_handler.py) | Turns project README changes into deterministic supervisor summaries. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_readme_handler.py |
| [src/reference_stub_enricher.py](../../../src/reference_stub_enricher.py) | Hash-checks and optionally enriches workspace reference stubs through the direct LLM path. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_reference_stub_enricher.py, tests/test_reference_stub_regeneration.py |
| [src/prompt_builder.py](../../../src/prompt_builder.py) | Orders roles, knowledge tiers, templates, context blocks, and tools into a prompt. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_prompt_builder.py, tests/test_l0_l1_tier_injection.py |
| [src/prompt_manager.py](../../../src/prompt_manager.py) | Discovers, parses, filters, and renders Markdown prompt templates. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | tests/test_prompt_manager.py |
| [src/prompts/memory_consolidation.py](../../../src/prompts/memory_consolidation.py) | Supplies prompt-construction text used for memory consolidation. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | prompt/memory focused tests |
| [src/prompts/memory_revision.py](../../../src/prompts/memory_revision.py) | Supplies prompt-construction text used when revising memory. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | prompt/memory focused tests |

## Shipped Markdown resources

These are shipped content rather than independently executing production modules. On
first setup, [src/vault.py](../../../src/vault.py) copies missing defaults into the
local vault and does not overwrite an existing local file.

| Resource family | Purpose | Component | Notes |
|---|---|---|---|
| [src/prompts/supervisor_system.md](../../../src/prompts/supervisor_system.md), [consolidation_task.md](../../../src/prompts/consolidation_task.md), [execution_focus.md](../../../src/prompts/execution_focus.md), and [plan_structure_guide.md](../../../src/prompts/plan_structure_guide.md) | Provide shipped prompt templates for supervisor, consolidation, and task-depth context. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | Loaded by PromptBuilder; tests/test_prompt_builder.py |
| [vault/templates/example-supervisor-runtime-profile.md](../../../vault/templates/example-supervisor-runtime-profile.md) and [reflection-playbook.md](../../../vault/templates/reflection-playbook.md) | Provide example vault authoring templates. | [Configuration, the vault, and prompt context](../../concepts/configuration-and-vault.md) | Shipped resources; local copies are user-owned after seeding. |

