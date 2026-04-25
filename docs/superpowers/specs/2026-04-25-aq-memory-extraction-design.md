---
date: 2026-04-25
status: draft
topic: aq-memory plugin extraction
---

# Extracting `memory_v2` to `aq-memory` (External Plugin)

## 1. Goal

Move the `memory_v2` internal plugin out of `agent-queue2/src/plugins/internal/memory_v2/` and into a standalone repository at `/mnt/d/Dev/aq/aq-memory`. After extraction:

- `agent-queue2` runs end-to-end with **no memory plugin installed**. Long-term memory features simply do not exist; everything else works.
- `aq-memory` is installed via the standard external plugin pathway (`aq plugin install <path>` or git URL).
- The `memsearch` package (currently in `agent-queue2/packages/memsearch/`) moves into the new repo. `agent-queue2` no longer depends on it.
- The plugin remains a `Plugin` (not `InternalPlugin`) — it operates with the sandboxed `PluginContext` API plus a small set of agreed-upon extension points.

## 2. Non-Goals

- Rewriting plugin internals. Code moves largely as-is.
- Changing the agent-facing tool surface (`memory_store`, `memory_recall`, etc.) — same names, same shapes.
- Splitting the plugin further (e.g., separating extractor from service) — single repo, single plugin.
- Publishing `memsearch` to PyPI — local + git installs only for now.
- Database migration of historical `memory_v2` plugin rows (acceptable to leave behind; new install registers under the new id).

## 3. Constraints

- `MemoryV2ServiceProtocol` already exists in `src/plugins/services.py`. Keep it as the contract.
- Most pull-style call sites already exist and access via `getattr(orch, "_memory_v2_service", None)`. Five call sites total:
  - `src/orchestrator/core.py:1138-1149` — wires service into facts watcher
  - `src/orchestrator/execution.py:454,456,466,478` — L1 facts, L1 guidance, L2 context
  - `src/supervisor.py:440` — same loading for supervisor prompt
  - `src/discord/commands.py` — memory stats display
  - `src/facts_handler.py` — receives service as constructor arg
- `MemoryExtractor` and `facts.md` watcher integration must keep working when the plugin is loaded; must silently no-op when it is not.

## 4. Target Architecture

### `agent-queue2` (after extraction)

```
agent-queue2/
├── src/
│   ├── plugins/
│   │   ├── services.py          # MemoryV2ServiceProtocol stays HERE (the contract)
│   │   ├── registry.py          # +get_service(name) → typed Protocol or None
│   │   └── internal/
│   │       └── (memory_v2/ removed)
│   └── facts_handler.py         # stays; service param remains optional (None when no plugin)
├── packages/
│   └── (memsearch/ removed)
└── pyproject.toml               # memsearch dep removed
```

### `aq-memory` (new repo)

```
aq-memory/
├── pyproject.toml               # name=aq-memory, entry point: aq.plugins.memory
├── aq_memory/
│   ├── __init__.py              # exports MemoryPlugin
│   ├── plugin.py                # was src/plugins/internal/memory_v2/plugin.py
│   ├── service.py               # was .../memory_v2/service.py
│   └── extractor.py             # was .../memory_v2/extractor.py
├── packages/
│   └── memsearch/               # vendored from agent-queue2
├── tests/
│   └── (~10 unit tests moved from agent-queue2/tests/)
├── CLAUDE.md                    # plugin agent guide
└── README.md
```

### Communication boundary

```
┌────────────────── agent-queue2 ──────────────────┐
│                                                  │
│  prompt_builder, supervisor, orchestrator,       │
│  facts_handler, discord/commands                 │
│         │                                        │
│         ▼                                        │
│  registry.get_service("memory_v2")               │
│         │                                        │
│         ▼ (returns MemoryV2ServiceProtocol|None) │
│                                                  │
└──────────────────────┬───────────────────────────┘
                       │
                       │ (only when plugin loaded)
                       ▼
┌──────────────────── aq-memory ───────────────────┐
│  MemoryPlugin → MemoryV2Service → memsearch      │
│  MemoryExtractor (subscribes via ctx.subscribe)  │
└──────────────────────────────────────────────────┘
```

## 5. Core Changes (`agent-queue2`)

### 5.1 New: `PluginRegistry.get_service(name)`

```python
# src/plugins/registry.py
def get_service(self, name: str) -> Any | None:
    """Return a service instance registered by a loaded plugin, or None.

    The service object should implement the corresponding Protocol in
    src/plugins/services.py (e.g., MemoryV2ServiceProtocol for "memory_v2").
    """
```

A plugin registers a service via `ctx.register_service("memory_v2", self.service)` during `initialize()`. The registry stores `{name: service}` and clears the entry on `unload`.

### 5.2 New: `PluginContext.register_service(name, instance)`

Already part of the family of registration methods (`register_command`, `register_tool`, `register_event_type`). One-line addition.

### 5.3 Replace `_memory_v2_service` direct access

Five call sites change from:
```python
mem_svc = getattr(self.orchestrator, "_memory_v2_service", None)
```
to:
```python
mem_svc = self.plugin_registry.get_service("memory_v2") if self.plugin_registry else None
```

The `_memory_v2_service` attribute is deleted.

### 5.4 Move facts.md watcher wiring out of orchestrator

Currently `orchestrator/core.py:1130-1149` reaches into the loaded plugin and calls `register_facts_handlers(self.vault_watcher, service=svc)`. After extraction, the plugin itself does this in `initialize()`:

```python
# In aq-memory's MemoryPlugin.initialize():
vault_watcher = ctx.get_service("vault_watcher")  # new tiny service
if vault_watcher:
    register_facts_handlers(vault_watcher, service=self.service)
```

This requires exposing `vault_watcher` as a service. `register_facts_handlers` itself stays in `agent-queue2/src/facts_handler.py` (it's the watcher-side glue) and accepts a `MemoryV2ServiceProtocol` arg.

### 5.5 Delete `src/plugins/internal/memory_v2/`

Including `__pycache__`. The internal plugin discovery scan no longer finds it.

### 5.6 `pyproject.toml`

Remove:
```toml
"memsearch[ollama] @ file:packages/memsearch",
```
And the `[memory]` extras notes referencing memsearch.

Delete `packages/memsearch/` directory.

### 5.7 Setup script: optional memory install

Add an interactive prompt to `setup.sh`:

```bash
read -p "Install aq-memory plugin? (y/N) " yn
if [[ $yn =~ ^[Yy]$ ]]; then
    aq plugin install /mnt/d/Dev/aq/aq-memory
fi
```

Also accept a non-interactive flag (`./setup.sh --with-memory`) for CI / scripted installs. Default is no — the goal is to prove agent-queue2 runs without memory by default.

## 6. Plugin Repo (`aq-memory`)

### 6.1 `pyproject.toml`

```toml
[project]
name = "aq-memory"
version = "0.1.0"
description = "Memory plugin for agent-queue (Milvus + memsearch backed)"
dependencies = [
    "memsearch[ollama] @ file:packages/memsearch",
]

[project.entry-points."aq.plugins"]
memory = "aq_memory:MemoryPlugin"

[build-system]
requires = ["setuptools>=78.1.1"]
build-backend = "setuptools.build_meta"
```

Plugin id becomes `memory` (not `memory_v2` or `aq-memory_v2`). The legacy compatibility lookup in `orchestrator/core.py:1140-1141` (`"aq-memory_v2") or get_plugin_instance("memory_v2")`) is removed entirely after extraction (replaced by `get_service("memory_v2")` which is content-addressed by Protocol contract, not plugin name).

### 6.2 Plugin class

```python
# aq_memory/plugin.py
class MemoryPlugin(Plugin):  # NOT InternalPlugin
    plugin_permissions = [PluginPermission.NETWORK, PluginPermission.FILESYSTEM]
    config_schema = {...}    # moved from current plugin

    async def initialize(self, ctx: PluginContext) -> None:
        # Build the service (was the plugin._service init code)
        self.service = MemoryV2Service(...)
        await self.service.initialize()

        # Expose as a named service for core consumers
        ctx.register_service("memory_v2", self.service)

        # Register agent-facing tools (unchanged)
        for tool_def in TOOL_DEFINITIONS:
            ctx.register_command(tool_def["name"], self._handle(tool_def["name"]))
            ctx.register_tool(tool_def)

        # Subscribe extractor to events
        self.extractor = MemoryExtractor(self.service, ...)
        ctx.subscribe("task.completed", self.extractor.on_task_completed)
        # ... other event subs

        # Wire facts watcher (if available)
        watcher = ctx.get_service("vault_watcher")
        if watcher:
            register_facts_handlers(watcher, service=self.service)

    async def shutdown(self, ctx: PluginContext) -> None:
        await self.service.shutdown()
```

### 6.3 PluginContext additions needed

Confirmed against current `src/plugins/base.py`:

- `register_service(name, instance)` — **does not exist; must be added**. Stores the instance in the registry's service map (separate from internal `_services`) and clears on plugin unload.
- `ctx.subscribe(event_type, handler)` — **already exists** at `base.py:362`. No trust gate. Use as-is for extractor's event hookups.
- `ctx.get_service(name)` — **exists, but raises `PermissionError` for `TrustLevel.EXTERNAL`** at `base.py:287-288`. Two required changes:
  1. Add a per-service allowlist concept. External plugins may call `get_service(name)` for services explicitly marked safe-for-external. Initial allowlist: `"config"`, `"vault_watcher"`. (No `db` / `git` / `workspace` for external plugins.)
  2. Add `vault_watcher` as a service in the first place — currently it's accessed via `self.orchestrator.vault_watcher` directly. Wrap with a `VaultWatcherService` Protocol exposing only `register_handler` / `unregister_handler` for facts.md path patterns.

The plugin-registered services from §5.1 (e.g., `register_service("memory_v2", svc)`) are looked up via the **PluginRegistry** (`registry.get_service(name)`), not via `PluginContext.get_service`. They are conceptually different namespaces:

- `PluginContext.get_service(name)` — services *core* exposes to plugins (db, config, etc.).
- `PluginRegistry.get_service(name)` — services *plugins* expose to core or to each other.

The two namespaces can collide on names but that's fine — they are accessed via different paths.

## 7. Tests

### Move to `aq-memory/tests/` (~10 files)
- test_memory_v2_service.py
- test_memory_extractor.py
- test_memory_save.py
- test_memory_promote.py
- test_memory_consolidation_playbook.py
- test_memory_consolidation_tools.py
- test_memory_audit_trail.py
- test_memory_health.py
- test_memory_contradiction.py
- test_memory_scope_alias.py
- test_stale_memory_detection.py

### Stay in `agent-queue2/tests/` (~2 contract tests)
- test_mcp_memory_roundtrip.py — MCP server + memory plugin integration; gate with `pytest.importorskip("aq_memory")`
- (New) test_memory_extension_points.py — verifies `registry.get_service("memory_v2")` returns `None` when plugin absent and L1 facts injection silently skips. This is the core contract test that proves "removable" works.

The aq-memory repo's CI installs agent-queue2 in editable mode to run its integration tests. agent-queue2's CI does NOT install aq-memory (proves the no-memory configuration works).

## 8. Removal Verification

The acceptance test for the entire extraction:

1. Fresh checkout of `agent-queue2`, `pip install -e ".[dev,cli]"`.
2. `pytest tests/ -n auto` passes with zero memory-related collection errors.
3. `./run.sh start` boots successfully with no errors about missing memory.
4. Agents run tasks; `memory_*` tools simply do not appear in the tool list.
5. Vault `facts.md` edits are detected by the watcher; no errors, just no KV sync.
6. Now `aq plugin install /mnt/d/Dev/aq/aq-memory` and restart — memory tools appear, L1 facts inject, watcher syncs facts to Milvus.

## 9. Migration Plan (Order)

1. **Branch in agent-queue2.** Create `extract-memory-plugin` branch.
2. **Build new repo skeleton.** `/mnt/d/Dev/aq/aq-memory` with pyproject, package dir, empty plugin file.
3. **Add `register_service` / `get_service`.** Land core changes (§5.1, §5.2) — purely additive, no behavior change.
4. **Switch call sites to `get_service`.** Five sites in §5.3. Internal plugin still in place; switching is a no-op behaviorally.
5. **Copy plugin source to `aq-memory`.** Adjust imports (`src.plugins.internal.memory_v2.X` → `aq_memory.X`). Make `Plugin` (not `InternalPlugin`).
6. **Move memsearch to `aq-memory/packages/memsearch/`.** Update `aq-memory/pyproject.toml` to depend on it.
7. **Move tests.** Per §7. Add the new contract test to agent-queue2.
8. **Install plugin locally**, run full agent-queue2 test suite + integration smoke. Verify identical behavior to pre-extraction.
9. **Delete `src/plugins/internal/memory_v2/`** and `packages/memsearch/` from agent-queue2. Drop memsearch dep from `pyproject.toml`. Remove the `aq-memory_v2`/`memory_v2` lookup fallbacks.
10. **Run no-memory acceptance test** (§8). Fix anything that breaks.
11. **Add setup-script prompt** for optional plugin install.
12. **Documentation pass.** Update `docs/specs/design/memory-plugin.md` to note plugin is now external. Update `docs/specs/plugin-system.md` Phase 5 status.

Steps 3, 4 land on `main` independently — they are safe refactors with the plugin still internal. Steps 5–12 are the actual extraction.

## 10. Risks & Open Questions

- **`get_service` for external plugins.** Need to confirm the existing service mechanism's trust model and decide which services external plugins can reach. If overly restrictive, this is the only place that meaningfully changes the plugin loader.
- **Async event subscription API.** Verify `PluginContext.subscribe_event` exists and works for external plugins. If not, the extractor's event hookup needs an alternative (e.g., emit-only with internal queue).
- **Embedded MCP server tool registration.** MCP server auto-exposes CommandHandler commands — confirm plugin commands flow through correctly when plugin loads.
- **Database `plugins` table cleanup.** Old installs will have a `memory_v2` row. Acceptable to leave it (status = error after deletion); not migrating.
- **`memsearch` install ergonomics.** A path-installed plugin depending on a vendored `packages/memsearch/` works locally; may need adjustment for a future git URL install (relative path resolves differently). Verify on local install first.

## 11. Out of scope (follow-ups)

- Publishing `memsearch` to PyPI.
- Publishing `aq-memory` to PyPI or registry.
- A second memory plugin implementation (the whole point of this extraction enables that, but isn't done here).
- Refactoring `facts_handler.py` to live elsewhere.
- Splitting `extractor.py` and `service.py` further.
