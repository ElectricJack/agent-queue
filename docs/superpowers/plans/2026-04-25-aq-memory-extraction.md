# Extract Memory Plugin to `aq-memory` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the memory plugin out of `agent-queue2/src/plugins/internal/memory_v2/` into a new standalone external plugin repo at `/mnt/d/Dev/aq/aq-memory`, dropping the vestigial `V2` suffix from all names. After this, agent-queue2 runs end-to-end with no memory plugin installed, and the plugin is installed via the standard external plugin pathway.

**Architecture:** Two-namespace service mechanism — plugin registers a service via `ctx.register_service("memory", svc)` (new), core consumers fetch via `registry.get_service("memory")` returning a `MemoryServiceProtocol | None`. Replaces the current `getattr(orch, "_memory_v2_service", None)` direct-attr pattern. External plugins gain narrow access to `vault_watcher` and `config` services via a per-service allowlist on `PluginContext.get_service`.

**Tech Stack:** Python 3.12+, pytest (asyncio auto), SQLAlchemy Core, Milvus (via memsearch fork), the existing `Plugin` / `PluginContext` / `PluginRegistry` infrastructure in `src/plugins/`.

---

## Spec Reference

This plan implements `docs/superpowers/specs/2026-04-25-aq-memory-extraction-design.md`. Section numbers below (e.g., "spec §5.0") refer to that document.

## Working Directory

Run all commands from `/home/jkern/dev/agent-queue2` unless explicitly noted (steps that operate on `/mnt/d/Dev/aq/aq-memory` say so).

## Branch

Use a single feature branch `extract-memory-plugin` for the entire plan. Each task commits independently. Push after groups of related tasks (after Task 2, after Task 6, after Task 12).

```bash
git checkout main
git pull
git checkout -b extract-memory-plugin
```

---

## Task 1: V2 Suffix Rename Pass (spec §5.0)

**Goal:** Rename `MemoryV2*` → `Memory*` and `memory_v2*` → `memory*` across all of agent-queue2. Pure mechanical rename, no behavior changes. The full test suite must pass before and after.

**Files (modified):** All 42 files containing `MemoryV2` / `memory_v2` / `V2_ONLY_TOOLS`. Source list:

```
src/plugins/internal/memory_v2/__init__.py        (will be moved in step 4)
src/plugins/internal/memory_v2/plugin.py          (will be moved in step 4)
src/plugins/internal/memory_v2/service.py         (will be moved in step 4)
src/plugins/internal/memory_v2/extractor.py       (will be moved in step 4)
src/plugins/services.py
src/plugins/registry.py
src/plugins/internal/notes.py
src/orchestrator/core.py
src/orchestrator/execution.py
src/supervisor.py
src/prompt_builder.py
src/discord/bot.py
src/discord/commands.py
src/commands/system_commands.py
src/tools/definitions.py
src/facts_handler.py
pyproject.toml
CLAUDE.md
profile.md
docs/agent-queue-primitives.md
docs/guides/architecture.md
docs/specs/design/roadmap.md
tests/test_memory_v2_service.py                   (will be renamed in step 6)
tests/test_memory_extractor.py
tests/test_memory_save.py
tests/test_memory_promote.py
tests/test_memory_consolidation_tools.py
tests/test_memory_audit_trail.py
tests/test_memory_health.py
tests/test_memory_contradiction.py
tests/test_memory_scope_alias.py
tests/test_stale_memory_detection.py
tests/test_mcp_memory_roundtrip.py
tests/test_facts_handler.py
tests/test_facts_bidirectional_sync.py
tests/test_topic_auto_detection.py
tests/test_summary_original_pattern.py
tests/test_l3_ondemand_search.py
tests/test_l0_l1_tier_injection.py
tests/test_mcp_server.py
tests/test_reflection_stale_contradiction.py
```

(Skip `docs/superpowers/specs/2026-04-25-aq-memory-extraction-design.md` — its `MemoryV2` / `memory_v2` mentions are intentionally preserved as the rename mapping and historical references.)

- [ ] **Step 1: Capture baseline test result**

```bash
pytest tests/ -n auto -q 2>&1 | tail -20
```

Expected: PASS (or document any pre-existing failures so they can be ignored). Save the count of passed tests for comparison after the rename.

- [ ] **Step 2: Rename `MemoryV2Service` → `MemoryService` (class, all references, docstrings)**

Includes the class definition in `src/plugins/internal/memory_v2/service.py` and every import / type annotation / docstring mention.

```bash
# Inspect first to make sure nothing surprising matches:
grep -r "MemoryV2Service" --include="*.py" --include="*.md" --include="*.toml" -l agent-queue2_skip=docs/superpowers/specs
```

Use the editor's project-wide rename, OR an `Edit` call per file with `replace_all=true`. Do NOT touch `docs/superpowers/specs/2026-04-25-aq-memory-extraction-design.md`.

After: verify no leftover occurrences except in the spec doc:

```bash
grep -rn "MemoryV2Service" --include="*.py" --include="*.md" --include="*.toml" . | grep -v "docs/superpowers/specs/"
```

Expected: zero results.

- [ ] **Step 3: Rename `MemoryV2ServiceProtocol` → `MemoryServiceProtocol`**

Same approach. Defined in `src/plugins/services.py:91`.

```bash
grep -rn "MemoryV2ServiceProtocol" --include="*.py" --include="*.md" . | grep -v "docs/superpowers/specs/"
```

Expected: zero results after rename.

- [ ] **Step 4: Rename `MemoryV2Plugin` → `MemoryPlugin`**

Defined in `src/plugins/internal/memory_v2/plugin.py:1241`. Re-exported from `src/plugins/internal/memory_v2/__init__.py:17`.

```bash
grep -rn "MemoryV2Plugin" --include="*.py" --include="*.md" . | grep -v "docs/superpowers/specs/"
```

Expected: zero results.

- [ ] **Step 5: Rename `_memory_v2_service` (orchestrator attribute) → `_memory_service`**

Defined in `src/orchestrator/core.py:1138`, used in `src/orchestrator/execution.py:454-478` and `src/supervisor.py:440`. Internal attribute name only — does not affect any external API.

```bash
grep -rn "_memory_v2_service" --include="*.py" . | grep -v "docs/superpowers/specs/"
```

Expected: zero results.

- [ ] **Step 6: Rename the `"memory_v2"` service map key**

In `src/plugins/services.py` change:
```python
services["memory_v2"] = memory_v2_service  # OLD
```
to:
```python
services["memory"] = memory_service  # NEW (param name also renamed)
```

Plus the parameter name `memory_v2_service` in the same function. Plus the module-level docstring listing services in `src/plugins/services.py:11`.

Update the legacy plugin lookup in `src/orchestrator/core.py:1140-1141`:
```python
mem_v2_plugin = self.plugin_registry.get_plugin_instance(
    "aq-memory_v2"
) or self.plugin_registry.get_plugin_instance("memory_v2")
```
becomes:
```python
mem_plugin = self.plugin_registry.get_plugin_instance("memory")
```

(The `aq-memory_v2` / `memory_v2` plugin-name fallback is deleted; the internal plugin will be named `"memory"` after Task 1 step 7.)

- [ ] **Step 7: Rename `src/plugins/internal/memory_v2/` directory → `src/plugins/internal/memory/`**

```bash
git mv src/plugins/internal/memory_v2 src/plugins/internal/memory
```

Then update every import that references `src.plugins.internal.memory_v2` → `src.plugins.internal.memory`:

```bash
grep -rn "src\.plugins\.internal\.memory_v2\|src/plugins/internal/memory_v2" --include="*.py" --include="*.md" .
```

Update each match. Internal plugin discovery at `src/plugins/internal/__init__.py` is name-agnostic (uses `pkgutil.iter_modules`), so the renamed package is automatically discovered.

- [ ] **Step 8: Drop the `V2_ONLY_TOOLS` alias**

In `src/plugins/internal/memory/plugin.py` (post-rename path):

```python
# Remove this line:
V2_ONLY_TOOLS = AGENT_TOOLS

# And remove from src/plugins/internal/memory/__init__.py imports/exports.
```

Check tests for usage — most likely `tests/test_memory_*.py` reference it. Replace with `AGENT_TOOLS` directly:

```bash
grep -rn "V2_ONLY_TOOLS" --include="*.py" .
```

Expected after step: zero results.

- [ ] **Step 9: Delete vestigial "V1 MemoryManager removed" comments**

Locations identified earlier:
- `src/orchestrator/core.py:296` — `# V1 MemoryManager removed (roadmap 8.6 — memory-plugin.md §2).`
- `src/orchestrator/core.py:1151` — `# V1 MemoryManager collection initialization removed (roadmap 8.6).`
- `src/prompt_builder.py:381` — `V1 MemoryManager integration removed (roadmap 8.6).` (inside docstring)
- `src/discord/bot.py:991` — same pattern in docstring
- `src/commands/system_commands.py:552` — `# Memory commands — V1 MemoryManager commands removed (roadmap 8.6).`
- `src/tools/definitions.py:1773` — `# recall_topic_context removed (roadmap 8.6 — v1 MemoryManager deleted).`

Delete each comment (or shorten to remove the V1 reference). History is in git; these are noise now.

Also strip "v1" / "v2" / "v2 architecture" mentions from the plugin's own module docstring (`src/plugins/internal/memory/plugin.py:1` and the class docstring at line 1241 and the comment at line 1253). E.g. "Memory v2: unified memory" → "Memory: unified memory".

Leave alone: literal data values like `{"value": "v2"}` in tool docstring examples (these are illustrative data, not version markers). Also leave the `aq-memory_v2` rename mapping in the spec doc (intentional historical reference).

- [ ] **Step 10: Update `pyproject.toml` comment**

`pyproject.toml:95` mentions `MemoryV2Service`. Edit to say `MemoryService`. The `pyproject.toml:49` comment about memsearch transitive dep stays as-is (not a V2 reference, just memsearch docs).

- [ ] **Step 11: Run full test suite**

```bash
pytest tests/ -n auto -q 2>&1 | tail -20
```

Expected: same pass count as Step 1 baseline. Any new failures are real bugs introduced by the rename — fix before continuing.

- [ ] **Step 12: Final sweep for stragglers**

```bash
grep -rn "MemoryV2\|memory_v2\|V2_ONLY_TOOLS" --include="*.py" --include="*.md" --include="*.toml" --include="*.yaml" . | grep -v "docs/superpowers/specs/"
```

Expected: zero results. If anything matches, decide whether it's intentional and fix if not.

- [ ] **Step 13: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Drop vestigial V2 suffix from memory plugin names

V1 MemoryManager was deleted in roadmap 8.6; the V2 suffix is now
vestigial. Mechanical rename across 42 files: MemoryV2Service ->
MemoryService, MemoryV2ServiceProtocol -> MemoryServiceProtocol,
MemoryV2Plugin -> MemoryPlugin, _memory_v2_service -> _memory_service,
"memory_v2" service key -> "memory", src/plugins/internal/memory_v2/ ->
src/plugins/internal/memory/. Drops V2_ONLY_TOOLS alias and stale
"V1 MemoryManager removed" comments.

No behavior changes. Pre-extraction rename per
docs/superpowers/specs/2026-04-25-aq-memory-extraction-design.md §5.0.
EOF
)"
```

---

## Task 2: Add `PluginContext.register_service` and `PluginRegistry.get_service`

**Goal:** Add the new service-registration API so plugins can expose a typed service to core (spec §5.1, §5.2). Pure addition — no existing behavior changes. TDD.

**Files:**
- Test: `tests/test_plugin_service_registration.py` (new)
- Modify: `src/plugins/base.py` — add `register_service` method on `PluginContext`
- Modify: `src/plugins/registry.py` — add `_plugin_services` dict, `register_plugin_service`, `get_service`
- Modify: `src/plugins/__init__.py` — re-export `MemoryServiceProtocol` for convenience

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plugin_service_registration.py`:

```python
"""Tests for the plugin-registered service mechanism (spec §5.1, §5.2).

Plugins register typed services that core consumers can fetch via
the registry.  Returns None if no plugin has registered the service.
"""

from __future__ import annotations

import pytest

from src.plugins.base import Plugin, PluginContext


class _DummyService:
    """Concrete service returned by a test plugin."""

    def __init__(self, label: str) -> None:
        self.label = label

    async def ping(self) -> str:
        return f"pong:{self.label}"


class _ProvidingPlugin(Plugin):
    plugin_name = "test-provider"

    async def initialize(self, ctx: PluginContext) -> None:
        ctx.register_service("test_service", _DummyService("hello"))

    async def shutdown(self, ctx: PluginContext) -> None:
        pass


@pytest.mark.asyncio
async def test_registered_service_visible_via_registry(plugin_registry_with_plugin):
    """A plugin's registered service is reachable via registry.get_service."""
    registry = await plugin_registry_with_plugin(_ProvidingPlugin)

    svc = registry.get_service("test_service")
    assert svc is not None
    assert isinstance(svc, _DummyService)
    assert await svc.ping() == "pong:hello"


@pytest.mark.asyncio
async def test_unregistered_service_returns_none(plugin_registry):
    """Asking for a service no plugin registered returns None, not raises."""
    assert plugin_registry.get_service("nonexistent") is None


@pytest.mark.asyncio
async def test_service_cleared_on_unload(plugin_registry_with_plugin):
    """Unloading the plugin removes its registered services."""
    registry = await plugin_registry_with_plugin(_ProvidingPlugin)
    assert registry.get_service("test_service") is not None

    await registry.unload_plugin("test-provider")
    assert registry.get_service("test_service") is None
```

Add the matching fixtures to `tests/conftest.py` if not already present. Look first — there are likely existing plugin-test fixtures to extend.

```python
# In tests/conftest.py — ADD if not present:

@pytest.fixture
async def plugin_registry(tmp_path, db, event_bus, app_config):
    """Bare PluginRegistry with no plugins loaded."""
    from src.plugins.registry import PluginRegistry

    registry = PluginRegistry(db=db, bus=event_bus, config=app_config)
    yield registry
    # No teardown needed — no plugins were loaded.


@pytest.fixture
async def plugin_registry_with_plugin(plugin_registry):
    """Helper that loads an in-memory plugin class into the registry."""
    async def _load(plugin_cls):
        await plugin_registry.register_in_memory_plugin(plugin_cls)
        return plugin_registry
    return _load
```

(If `register_in_memory_plugin` does not exist yet on the registry, add a minimal helper as part of the next step — or use the existing internal-plugin loading pathway. Pick whichever matches existing test conventions; check `tests/test_plugin_*.py` for prior art.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_plugin_service_registration.py -v
```

Expected: FAIL with `AttributeError: PluginContext has no attribute 'register_service'` or similar.

- [ ] **Step 3: Implement `PluginContext.register_service`**

In `src/plugins/base.py`, after `register_event_type` (around line 348), add:

```python
def register_service(self, name: str, instance: object) -> None:
    """Expose a service the plugin provides for core / other plugins to use.

    The instance should implement the corresponding Protocol declared in
    ``src/plugins/services.py`` (e.g. :class:`MemoryServiceProtocol` for
    ``name="memory"``).  Core consumers fetch it via
    ``plugin_registry.get_service(name)`` and it returns ``None`` when no
    plugin has registered the service.

    Args:
        name: Service name (e.g. ``"memory"``).  Conventionally matches
              the Protocol it implements.
        instance: The service object.  Stored as-is; lifecycle is the
                  plugin's responsibility.
    """
    if not name:
        raise ValueError("Service name must be non-empty")
    self._plugin_services_callback(self._plugin_name, name, instance)
    self._logger.debug("Registered service: %s", name)
```

Add `_plugin_services_callback` to the constructor signature (just before `active_project_id_getter`):

```python
plugin_services_callback: Callable[[str, str, object], None] | None = None,
```

Store it: `self._plugin_services_callback = plugin_services_callback or (lambda *_: None)`.

The callback is the indirection — `PluginContext` does not own the registry. The registry passes its own bound method when constructing the context.

- [ ] **Step 4: Implement `PluginRegistry.get_service` and `register_plugin_service`**

In `src/plugins/registry.py`, find the `__init__` of `PluginRegistry` (search for `class PluginRegistry`) and add:

```python
# In __init__ — alongside the existing service / capability dicts:
self._plugin_services: dict[str, object] = {}
self._plugin_services_by_owner: dict[str, set[str]] = {}
```

Then add two methods on the class (placement: near `get_plugin_instance` at line 1023):

```python
def register_plugin_service(
    self, plugin_name: str, service_name: str, instance: object
) -> None:
    """Internal: called by PluginContext.register_service."""
    if service_name in self._plugin_services:
        existing_owner = next(
            (o for o, names in self._plugin_services_by_owner.items()
             if service_name in names),
            None,
        )
        if existing_owner and existing_owner != plugin_name:
            logger.warning(
                "Plugin %r overrides service %r previously registered by %r",
                plugin_name, service_name, existing_owner,
            )
    self._plugin_services[service_name] = instance
    self._plugin_services_by_owner.setdefault(plugin_name, set()).add(service_name)

def get_service(self, name: str) -> object | None:
    """Return a service instance registered by a loaded plugin, or None.

    The service object should implement the corresponding Protocol in
    src/plugins/services.py (e.g. MemoryServiceProtocol for "memory").
    """
    return self._plugin_services.get(name)

def _clear_plugin_services(self, plugin_name: str) -> None:
    """Internal: clear all services owned by a plugin (called on unload)."""
    owned = self._plugin_services_by_owner.pop(plugin_name, set())
    for name in owned:
        self._plugin_services.pop(name, None)
```

Wire `_clear_plugin_services` into `unload_plugin` — find the existing `unload_plugin` method and add a call to `self._clear_plugin_services(plugin_name)` near where commands/tools are de-registered.

Wire the callback into context construction — find every place `PluginContext(...)` is constructed (likely 1–2 spots in `registry.py`, search for `PluginContext(`). Pass:

```python
plugin_services_callback=self.register_plugin_service,
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_plugin_service_registration.py -v
```

Expected: all three tests PASS.

- [ ] **Step 6: Run full test suite to verify no regressions**

```bash
pytest tests/ -n auto -q 2>&1 | tail -20
```

Expected: same pass count as Task 1 baseline.

- [ ] **Step 7: Commit**

```bash
git add tests/test_plugin_service_registration.py tests/conftest.py src/plugins/base.py src/plugins/registry.py
git commit -m "$(cat <<'EOF'
Add register_service / get_service for plugin-provided services

PluginContext.register_service(name, instance) lets a plugin expose a
typed service object during initialize().  PluginRegistry.get_service(name)
returns the instance (or None) for core consumers like prompt_builder
and supervisor.  Services are cleared automatically on plugin unload.

Two namespaces deliberately:
  PluginContext.get_service  - services core exposes to plugins
  PluginRegistry.get_service - services plugins expose to core

Spec §5.1, §5.2.
EOF
)"
```

---

## Task 3: Loosen `PluginContext.get_service` for External Plugins + Add `vault_watcher` Service

**Goal:** External plugins currently get `PermissionError` on any `get_service` call. Loosen this with a per-service allowlist, and expose a new `vault_watcher` service so the memory plugin can register facts handlers itself (spec §5.4, §6.3). TDD.

**Files:**
- Test: `tests/test_plugin_external_service_access.py` (new)
- Test: `tests/test_vault_watcher_service.py` (new)
- Modify: `src/plugins/base.py` — replace blanket `EXTERNAL` reject with allowlist
- Modify: `src/plugins/services.py` — add `VaultWatcherService` Protocol + impl
- Modify: `src/plugins/registry.py` — register the `vault_watcher` service in `_build_internal_services` (renaming method appropriately) and pass it to external contexts
- Modify: `src/orchestrator/core.py` — pass `vault_watcher` into the registry's service builder

- [ ] **Step 1: Write the failing tests for the allowlist**

Create `tests/test_plugin_external_service_access.py`:

```python
"""External plugins may access an explicit allowlist of services.

Spec §6.3 — config and vault_watcher are safe-for-external; db, git,
workspace remain INTERNAL-only.
"""

from __future__ import annotations

import pytest

from src.plugins.base import Plugin, PluginContext, TrustLevel


@pytest.fixture
def external_context(plugin_context_factory):
    return plugin_context_factory(
        trust_level=TrustLevel.EXTERNAL,
        services={
            "config": object(),
            "vault_watcher": object(),
            "db": object(),
            "git": object(),
        },
    )


def test_external_can_get_config(external_context):
    assert external_context.get_service("config") is not None


def test_external_can_get_vault_watcher(external_context):
    assert external_context.get_service("vault_watcher") is not None


def test_external_cannot_get_db(external_context):
    with pytest.raises(PermissionError, match="vault_watcher|config"):
        external_context.get_service("db")


def test_external_cannot_get_git(external_context):
    with pytest.raises(PermissionError):
        external_context.get_service("git")


def test_internal_can_get_db(plugin_context_factory):
    ctx = plugin_context_factory(
        trust_level=TrustLevel.INTERNAL,
        services={"db": object(), "git": object()},
    )
    assert ctx.get_service("db") is not None
    assert ctx.get_service("git") is not None
```

Add the `plugin_context_factory` fixture in `tests/conftest.py` if not present. Check first; if a similar fixture exists, extend it. Skeleton:

```python
@pytest.fixture
def plugin_context_factory(tmp_path, db, event_bus):
    """Build a PluginContext with the given trust_level / services for unit tests."""
    from src.plugins.base import PluginContext, TrustLevel

    def _make(*, trust_level: TrustLevel = TrustLevel.EXTERNAL, services: dict | None = None):
        return PluginContext(
            plugin_name="testplugin",
            install_path=str(tmp_path / "install"),
            data_path=str(tmp_path / "data"),
            db=db,
            bus=event_bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
            trust_level=trust_level,
            services=services or {},
        )
    return _make
```

- [ ] **Step 2: Run failing tests**

```bash
pytest tests/test_plugin_external_service_access.py -v
```

Expected: most tests FAIL (everything except `test_internal_can_get_db`).  `test_external_can_get_config` and `test_external_can_get_vault_watcher` will fail with `PermissionError`.

- [ ] **Step 3: Implement the allowlist**

In `src/plugins/base.py`, replace the body of `get_service` (currently at lines 264-289):

```python
# Add at module scope (top of file, near TrustLevel definition):
EXTERNAL_ALLOWED_SERVICES: frozenset[str] = frozenset({
    "config",
    "vault_watcher",
})


# Replace the get_service method:
def get_service(self, name: str) -> Any:
    """Get a service by name.

    Internal plugins (``TrustLevel.INTERNAL``) can access all services.
    External plugins can only access services in
    :data:`EXTERNAL_ALLOWED_SERVICES`.

    Args:
        name: Service name.

    Returns:
        The service object.

    Raises:
        ValueError: If the service is unknown.
        PermissionError: If the plugin lacks access.
    """
    service = self._services.get(name)
    if service is None:
        available = list(self._services.keys())
        raise ValueError(f"Unknown service: {name!r}. Available: {available}")
    if (
        self._trust_level == TrustLevel.EXTERNAL
        and name not in EXTERNAL_ALLOWED_SERVICES
    ):
        raise PermissionError(
            f"Service {name!r} is not in the external allowlist "
            f"{sorted(EXTERNAL_ALLOWED_SERVICES)}; requires TrustLevel.INTERNAL"
        )
    return service
```

- [ ] **Step 4: Run tests for allowlist behavior**

```bash
pytest tests/test_plugin_external_service_access.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Write failing test for the `VaultWatcherService` shape**

Create `tests/test_vault_watcher_service.py`:

```python
"""The vault_watcher service is the safe-for-external Protocol facade."""

from __future__ import annotations

import pytest

from src.plugins.services import VaultWatcherService


class _StubVaultWatcher:
    """Minimal duck-typed VaultWatcher for the Protocol check."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, object, str | None]] = []
        self.unregistered: list[str] = []

    def register_handler(self, pattern, handler, *, handler_id=None):
        self.registered.append((pattern, handler, handler_id))
        return handler_id or f"auto:{pattern}"

    def unregister_handler(self, handler_id):
        self.unregistered.append(handler_id)


def test_vault_watcher_service_protocol_satisfied_by_stub():
    """VaultWatcherService is a runtime_checkable Protocol covering register/unregister."""
    stub = _StubVaultWatcher()
    assert isinstance(stub, VaultWatcherService)


def test_vault_watcher_service_registers_and_unregisters():
    """The Protocol contract round-trips through the stub."""
    svc: VaultWatcherService = _StubVaultWatcher()

    async def _handler(_): pass

    hid = svc.register_handler("FACTS.md", _handler, handler_id="facts:FACTS.md")
    assert hid == "facts:FACTS.md"

    svc.unregister_handler(hid)
    assert hid in svc.unregistered  # type: ignore[attr-defined]
```

- [ ] **Step 6: Run failing test**

```bash
pytest tests/test_vault_watcher_service.py -v
```

Expected: FAIL with `ImportError: cannot import name 'VaultWatcherService'`.

- [ ] **Step 7: Add the `VaultWatcherService` Protocol**

In `src/plugins/services.py`, add at the end of the Protocols section (after `ConfigService`, around line 244):

```python
@runtime_checkable
class VaultWatcherService(Protocol):
    """Register / unregister handlers for vault file changes.

    Exposed to external plugins so they can react to vault edits
    (e.g., the memory plugin syncs facts.md changes to its KV store).
    """

    def register_handler(
        self,
        pattern: str,
        handler: Callable[[list[Any]], Any],
        *,
        handler_id: str | None = None,
    ) -> str: ...

    def unregister_handler(self, handler_id: str) -> None: ...
```

Make sure `Callable` is imported at the top of the file (it likely is via `typing` or `collections.abc`; add if not).

Update the module-level docstring at the top of `services.py` to add:
```
- ``"vault_watcher"`` — :class:`VaultWatcherService` (external-allowed)
```

- [ ] **Step 8: Run vault watcher tests**

```bash
pytest tests/test_vault_watcher_service.py -v
```

Expected: PASS.

- [ ] **Step 9: Wire `vault_watcher` as a service in the registry**

Two parts:

**Part A** — In `src/plugins/registry.py`, find `_build_internal_services` (line 332). It currently builds services for INTERNAL plugins. We want it to also include `vault_watcher` for external. The simplest approach: include `vault_watcher` in the dict it returns, since the per-call allowlist in `PluginContext.get_service` enforces what external can actually fetch. Add a `vault_watcher=None` param to the method (or to `set_internal_services`):

```python
def set_internal_services(self, services: dict) -> None:
    """Provide service implementations after orchestrator init."""
    self._internal_services = services
    # Replay onto already-loaded contexts whose trust matches.
    for loaded in self._loaded.values():
        # Internal plugins get the whole map; external get only allowlisted services.
        if loaded.context._trust_level == TrustLevel.INTERNAL:
            loaded.context._services = services
        else:
            from src.plugins.base import EXTERNAL_ALLOWED_SERVICES
            loaded.context._services = {
                k: v for k, v in services.items() if k in EXTERNAL_ALLOWED_SERVICES
            }
```

Apply the same filter at context-construction time in the registry (find every `PluginContext(...)` call site and pass `services=` filtered by allowlist when trust_level is EXTERNAL).

**Part B** — In `src/orchestrator/core.py`, find where `set_internal_services` (or equivalent) is called after the orchestrator's `vault_watcher` is initialized. Add `vault_watcher` to that dict. Search for it:

```bash
grep -n "set_internal_services\|_build_internal_services" src/orchestrator/core.py
```

Pass `vault_watcher=self.vault_watcher` (the existing attr) into the services dict. The exact call shape depends on what you find — keep the existing pattern.

- [ ] **Step 10: Run full test suite**

```bash
pytest tests/ -n auto -q 2>&1 | tail -20
```

Expected: same pass count as before. If anything fails, the fixture changes or `_services` filtering may have hit a test that constructs a context directly — fix the test to either pass `trust_level=TrustLevel.INTERNAL` or include only allowlisted services.

- [ ] **Step 11: Commit**

```bash
git add tests/test_plugin_external_service_access.py tests/test_vault_watcher_service.py tests/conftest.py src/plugins/base.py src/plugins/services.py src/plugins/registry.py src/orchestrator/core.py
git commit -m "$(cat <<'EOF'
Allow external plugins to use config and vault_watcher services

Replaces the blanket EXTERNAL service-access reject with a per-service
allowlist (config, vault_watcher).  Adds VaultWatcherService Protocol so
external plugins can register file-watch handlers without raw access to
the orchestrator's vault watcher.  db, git, workspace remain
INTERNAL-only.

Spec §5.4, §6.3.
EOF
)"
```

---

## Task 4: Switch Core Call Sites to `registry.get_service("memory")`

**Goal:** Replace the five `getattr(orch, "_memory_service", None)` direct-attribute lookups with `plugin_registry.get_service("memory")` (spec §5.3). The internal memory plugin still exists and now also registers itself as `"memory"` via the new mechanism — switching is a no-op behaviorally.

**Files:**
- Modify: `src/plugins/internal/memory/plugin.py` — call `ctx.register_service("memory", self.service)` in `initialize`
- Modify: `src/orchestrator/execution.py:454` — switch to `registry.get_service`
- Modify: `src/supervisor.py:440` — same
- Modify: `src/orchestrator/core.py:1138-1149` — facts watcher wiring (still works via the same plugin instance, but uses the new service lookup)
- Modify: `src/discord/commands.py` — memory stats display (find via `grep -n "_memory_service\|_memory_v2_service" src/discord/commands.py` post-rename)
- Test: `tests/test_memory_extension_points.py` (new — contract test that proves removability)

- [ ] **Step 1: Write failing contract test**

Create `tests/test_memory_extension_points.py`:

```python
"""Contract tests proving the memory plugin is reachable via the
extension points and absent-cleanly when no plugin registers itself.

Spec §8 acceptance test.
"""

from __future__ import annotations

import pytest

from src.plugins.base import Plugin, PluginContext


class _StubMemoryService:
    """Minimal stub matching the parts of MemoryServiceProtocol prompt_builder uses."""

    @property
    def available(self) -> bool:
        return True

    async def load_l1_facts(self, *, project_id, agent_type):
        return f"l1-facts:{project_id}:{agent_type}"

    async def load_l1_guidance(self, *, project_id, agent_type):
        return ""

    async def load_l2_context(self, *, project_id, agent_type, **_):
        return ""


class _StubMemoryPlugin(Plugin):
    plugin_name = "memory"

    async def initialize(self, ctx: PluginContext) -> None:
        ctx.register_service("memory", _StubMemoryService())

    async def shutdown(self, ctx: PluginContext) -> None:
        pass


@pytest.mark.asyncio
async def test_get_service_returns_none_when_no_plugin(plugin_registry):
    """With no memory plugin loaded, registry.get_service('memory') is None."""
    assert plugin_registry.get_service("memory") is None


@pytest.mark.asyncio
async def test_get_service_returns_stub_when_plugin_loaded(
    plugin_registry_with_plugin,
):
    registry = await plugin_registry_with_plugin(_StubMemoryPlugin)
    svc = registry.get_service("memory")
    assert svc is not None
    assert await svc.load_l1_facts(project_id="p1", agent_type="supervisor") == (
        "l1-facts:p1:supervisor"
    )


@pytest.mark.asyncio
async def test_l1_facts_skipped_when_memory_absent(
    orchestrator_without_memory_plugin, prompt_builder_for_project
):
    """When memory plugin is absent, prompt_builder L1 facts section is empty
    and no errors are raised."""
    builder = await prompt_builder_for_project(
        orchestrator_without_memory_plugin,
        project_id="p1",
    )
    assembled = builder.assemble()
    assert "l1-facts:" not in assembled  # nothing was injected


@pytest.mark.asyncio
async def test_l1_facts_injected_when_memory_present(
    orchestrator_with_stub_memory_plugin, prompt_builder_for_project
):
    builder = await prompt_builder_for_project(
        orchestrator_with_stub_memory_plugin,
        project_id="p1",
    )
    assembled = builder.assemble()
    assert "l1-facts:p1:supervisor" in assembled
```

The fixtures `orchestrator_without_memory_plugin`, `orchestrator_with_stub_memory_plugin`, and `prompt_builder_for_project` need to be added to `tests/conftest.py`. They should:

- Construct an `Orchestrator` (or its minimal slice) with a `PluginRegistry`.
- For `orchestrator_with_stub_memory_plugin`, also load `_StubMemoryPlugin`.
- `prompt_builder_for_project` should return whatever entry point currently builds the supervisor prompt (look at `src/supervisor.py:430-470` to see how it's done in production — replicate the slice the test needs).

If full orchestrator construction is too heavy, narrow the test to just the prompt-builder code path: call `PromptBuilder` directly and have it use `registry.get_service("memory")` instead of `getattr(orch, ...)`. Pick whichever maps cleanest to existing test patterns.

- [ ] **Step 2: Run failing test**

```bash
pytest tests/test_memory_extension_points.py -v
```

Expected: at minimum, the first three tests fail because the memory plugin doesn't yet call `register_service`. The L1 facts tests may fail for the same reason or because the call site still uses `getattr`.

- [ ] **Step 3: Have the (still-internal) memory plugin call `register_service`**

In `src/plugins/internal/memory/plugin.py`, find the `initialize` method (it's the standard `Plugin` initialize hook — search for `async def initialize`). After the existing service init, add:

```python
# Expose the service via the new plugin-services registry so core
# consumers (prompt_builder, supervisor, facts watcher) can reach it
# without direct orchestrator attribute access.
if self._service is not None:
    ctx.register_service("memory", self._service)
```

(Variable name may differ — match the existing `self._service` / `self.service` convention in the file.)

- [ ] **Step 4: Switch `src/orchestrator/execution.py` call sites**

Replace each occurrence of:

```python
if self._memory_service:
    ...
    l1_text = await self._memory_service.load_l1_facts(...)
```

with:

```python
mem_svc = self.plugin_registry.get_service("memory") if self.plugin_registry else None
if mem_svc:
    ...
    l1_text = await mem_svc.load_l1_facts(...)
```

(Apply for all four sites in `execution.py:454, 456, 466, 478`.)

Be careful: the four sites likely share a single `if self._memory_service:` block. Refactor once, capture into a local `mem_svc`, reuse.

- [ ] **Step 5: Switch `src/supervisor.py:440`**

Same pattern. The existing code at `src/supervisor.py:440`:

```python
mem_svc = getattr(self.orchestrator, "_memory_service", None)
```

becomes:

```python
mem_svc = (
    self.orchestrator.plugin_registry.get_service("memory")
    if getattr(self.orchestrator, "plugin_registry", None) else None
)
```

- [ ] **Step 6: Switch `src/discord/commands.py` memory stats display**

Find the call site:

```bash
grep -n "_memory_service\|memory_service" src/discord/commands.py
```

Replace the same way: `registry.get_service("memory")` instead of attribute access. The Discord command likely has access to `self.orchestrator` or `self.bot.orchestrator` — adapt accordingly.

- [ ] **Step 7: Switch `src/orchestrator/core.py:1138-1149` facts watcher wiring**

Currently:

```python
self._memory_service = None
mem_plugin = self.plugin_registry.get_plugin_instance("memory")
if mem_plugin:
    svc = getattr(mem_plugin, "service", None)
    if svc and getattr(svc, "available", False):
        from src.facts_handler import register_facts_handlers
        register_facts_handlers(self.vault_watcher, service=svc)
        self._memory_service = svc
        logger.info("Wired MemoryService to facts.md watcher handlers")
```

Replace with:

```python
mem_svc = self.plugin_registry.get_service("memory")
if mem_svc and getattr(mem_svc, "available", False):
    from src.facts_handler import register_facts_handlers
    register_facts_handlers(self.vault_watcher, service=mem_svc)
    logger.info("Wired memory service to facts.md watcher handlers")
```

The `self._memory_service = ...` attribute assignment is GONE — no other code reads it after the switches in steps 4-6.

(Note: the facts-watcher wiring still lives in `core.py` for now. Task 6 step 4 will move it into the plugin's `initialize` after the plugin is external.)

- [ ] **Step 8: Run full test suite**

```bash
pytest tests/ -n auto -q 2>&1 | tail -20
```

Expected: same pass count. The new contract tests in `test_memory_extension_points.py` should now pass (the internal memory plugin still loads, registers `"memory"`, and the call sites pick it up).

- [ ] **Step 9: Smoke test the full app**

```bash
./run.sh start
# Wait for "Orchestrator started" log line.
# Issue a Discord command that triggers memory_recall, OR run a small task.
# Check logs for "Wired memory service to facts.md watcher handlers".
./run.sh stop
```

Expected: no errors related to memory. L1 facts injection and memory tools work as before.

- [ ] **Step 10: Commit**

```bash
git add tests/test_memory_extension_points.py tests/conftest.py src/plugins/internal/memory/plugin.py src/orchestrator/execution.py src/orchestrator/core.py src/supervisor.py src/discord/commands.py
git commit -m "$(cat <<'EOF'
Switch memory consumers to registry.get_service('memory')

prompt_builder (via execution.py), supervisor.py, discord/commands.py,
and the facts-watcher wiring in orchestrator/core.py now use
plugin_registry.get_service('memory') instead of the direct
_memory_service attribute on the orchestrator.

The internal memory plugin registers itself via
ctx.register_service('memory', self.service) during initialize().
End behavior is identical; this is the contract shift that lets the
plugin become external without touching the call sites again.

Adds tests/test_memory_extension_points.py — the contract test
proving (a) registry.get_service('memory') returns None when no
plugin is loaded and (b) prompt-builder L1 facts injection silently
no-ops in that case.  This is the spec §8 acceptance criterion in
test form.

Spec §5.3.
EOF
)"
```

---

## Task 5: Build `aq-memory` Repo Skeleton

**Goal:** Create the new repo at `/mnt/d/Dev/aq/aq-memory` with the right Python packaging shape, an empty plugin shell, and a CLAUDE.md. No code moves yet; this is just the empty container.

**Files (created in `/mnt/d/Dev/aq/aq-memory`):**
- `pyproject.toml`
- `aq_memory/__init__.py`
- `aq_memory/plugin.py` (skeleton)
- `CLAUDE.md`
- `README.md`
- `.gitignore`
- `tests/__init__.py`

- [ ] **Step 1: Create the directory and initialize git**

```bash
mkdir -p /mnt/d/Dev/aq/aq-memory
cd /mnt/d/Dev/aq/aq-memory
git init
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "aq-memory"
version = "0.1.0"
description = "Memory plugin for agent-queue (Milvus + memsearch backed)"
requires-python = ">=3.12"
dependencies = [
    "memsearch[ollama] @ file:packages/memsearch",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "pytest-xdist>=3.5"]

[project.entry-points."aq.plugins"]
memory = "aq_memory:MemoryPlugin"

[build-system]
requires = ["setuptools>=78.1.1"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["."]
include = ["aq_memory*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 3: Write `aq_memory/__init__.py`**

```python
"""Memory plugin for agent-queue.

Provides agent-facing memory tools (store, recall, delete, etc.),
maintains a Milvus-backed semantic memory and KV store via the
memsearch fork, and runs an event-driven memory extractor.

Exposed via the `aq.plugins` entry point as `memory`.
"""

from aq_memory.plugin import MemoryPlugin

__all__ = ["MemoryPlugin"]
```

- [ ] **Step 4: Write `aq_memory/plugin.py` skeleton**

Just enough to be importable and pass loader validation. Real code lands in Task 6.

```python
"""Memory plugin entry point.

Implementation moves here from agent-queue2/src/plugins/internal/memory/
in Task 6 of the extraction plan.
"""

from __future__ import annotations

import logging

# This import path will be a versioned external dep once aq-memory is
# installed; agent-queue2 must be on the import path (editable install).
from src.plugins.base import Plugin, PluginContext, PluginPermission

logger = logging.getLogger(__name__)


class MemoryPlugin(Plugin):
    """Memory plugin (skeleton — implementation lands in Task 6)."""

    plugin_permissions = [PluginPermission.NETWORK, PluginPermission.FILESYSTEM]

    async def initialize(self, ctx: PluginContext) -> None:
        logger.warning("MemoryPlugin skeleton loaded — no functionality yet.")

    async def shutdown(self, ctx: PluginContext) -> None:
        pass
```

- [ ] **Step 5: Write `CLAUDE.md`**

```markdown
# aq-memory

Memory plugin for agent-queue. Provides agent-facing memory tools (store, recall,
delete, etc.) and a Milvus-backed semantic memory + KV store via the
memsearch fork.

## Structure

- `aq_memory/plugin.py` — `MemoryPlugin` class; agent-facing tool handlers
- `aq_memory/service.py` — `MemoryService`: async wrapper over memsearch / Milvus
- `aq_memory/extractor.py` — `MemoryExtractor`: event-driven background extraction
- `packages/memsearch/` — vendored memsearch fork (Milvus + scope routing)
- `tests/` — plugin unit and integration tests

## Development

Install editable, alongside an editable agent-queue2 checkout:

```bash
pip install -e ../agent-queue2
pip install -e .
```

Then register the plugin with a running agent-queue:

```bash
aq plugin install /mnt/d/Dev/aq/aq-memory
```

Run tests:

```bash
pytest tests/ -n auto
```

## How It Wires Into agent-queue2

- Registers a single service `"memory"` (implementing `MemoryServiceProtocol`)
  via `ctx.register_service`. Core consumers fetch it via
  `plugin_registry.get_service("memory")`.
- Subscribes to events via `ctx.subscribe(...)` for the extractor
  (`task.completed`, etc.).
- Calls `ctx.get_service("vault_watcher")` to register handlers for
  `facts.md` changes (KV sync).

## Commands / Tools Provided

Agent-facing tool surface (registered via `register_tool`):
`memory_store`, `memory_recall`, `memory_delete`, `memory_follow`,
`memory_update`, `memory_promote`, `memory_promote_to_knowledge`,
`memory_search`.

## Configuration

See `MemoryPlugin.config_schema` in `aq_memory/plugin.py`. Instance
config lives at `~/.agent-queue/plugins/memory/config.yaml`.

## Testing

```bash
pytest tests/ -n auto                 # full suite
pytest tests/test_memory_service.py   # service-only
```

Integration tests rely on Milvus Lite (file-backed). No external
infrastructure required.
```

- [ ] **Step 6: Write `README.md`**

```markdown
# aq-memory

Memory plugin for [agent-queue](https://github.com/jackkern/agent-queue).
Provides Milvus-backed semantic memory and a KV store, exposed to agents
via tools like `memory_store` and `memory_recall`.

## Install

```bash
aq plugin install /mnt/d/Dev/aq/aq-memory
# OR
aq plugin install https://github.com/<owner>/aq-memory.git
```

See `CLAUDE.md` for development.
```

- [ ] **Step 7: Write `.gitignore`**

```
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.venv/
build/
dist/
.coverage
*.db
```

- [ ] **Step 8: Commit the skeleton**

```bash
cd /mnt/d/Dev/aq/aq-memory
git add -A
git commit -m "Initial aq-memory plugin skeleton"
```

(The agent-queue2 working tree is not touched in this task.)

---

## Task 6: Move Plugin Source from agent-queue2 to aq-memory

**Goal:** Copy the four memory plugin source files into `aq_memory/`, adjust imports, change `InternalPlugin` → `Plugin`, and have the new external plugin do everything the internal one did (register service, register tools, subscribe events, wire facts watcher).

**Files:**
- Create: `/mnt/d/Dev/aq/aq-memory/aq_memory/plugin.py` (replacing skeleton; was `src/plugins/internal/memory/plugin.py`)
- Create: `/mnt/d/Dev/aq/aq-memory/aq_memory/service.py` (was `src/plugins/internal/memory/service.py`)
- Create: `/mnt/d/Dev/aq/aq-memory/aq_memory/extractor.py` (was `src/plugins/internal/memory/extractor.py`)
- Modify: `/mnt/d/Dev/aq/aq-memory/aq_memory/__init__.py` (re-exports as needed)

**Imports to rewrite in the copies (in all three files):**

| Old | New |
|---|---|
| `from src.plugins.internal.memory.service import MemoryService` | `from aq_memory.service import MemoryService` |
| `from src.plugins.internal.memory.extractor import MemoryExtractor` | `from aq_memory.extractor import MemoryExtractor` |
| `from src.plugins.internal.memory.plugin import ...` | `from aq_memory.plugin import ...` |
| `from src.plugins.base import InternalPlugin, PluginContext` | `from src.plugins.base import Plugin, PluginContext, PluginPermission` |
| `class MemoryPlugin(InternalPlugin):` | `class MemoryPlugin(Plugin):` |
| (any `_internal = True` class attr) | delete |

Other imports of `src.*` (e.g. `src.facts_handler`, `src.event_bus` types, `src.config`) remain — they reach into agent-queue2 via the editable install on the import path. That's expected.

- [ ] **Step 1: Copy the three source files**

```bash
cp /home/jkern/dev/agent-queue2/src/plugins/internal/memory/service.py \
   /mnt/d/Dev/aq/aq-memory/aq_memory/service.py

cp /home/jkern/dev/agent-queue2/src/plugins/internal/memory/extractor.py \
   /mnt/d/Dev/aq/aq-memory/aq_memory/extractor.py

cp /home/jkern/dev/agent-queue2/src/plugins/internal/memory/plugin.py \
   /mnt/d/Dev/aq/aq-memory/aq_memory/plugin.py
```

This OVERWRITES the skeleton plugin.py from Task 5. That's intentional.

- [ ] **Step 2: Rewrite imports in `aq_memory/service.py`**

```bash
cd /mnt/d/Dev/aq/aq-memory
# Replace internal imports with package-relative ones.
sed -i 's|src\.plugins\.internal\.memory\.|aq_memory.|g' aq_memory/service.py
```

Then read it and verify:

```bash
grep -n "from src\.\|import src\." aq_memory/service.py | head -20
```

Remaining `from src.*` lines should be only for genuine agent-queue2 dependencies (e.g. `from src.config import AppConfig` — acceptable). Anything else needs manual review.

Replace `from src.plugins.base import InternalPlugin` with `from src.plugins.base import Plugin` if present in `service.py` (likely not — `service.py` is just the service class).

- [ ] **Step 3: Rewrite imports in `aq_memory/extractor.py`**

```bash
sed -i 's|src\.plugins\.internal\.memory\.|aq_memory.|g' aq_memory/extractor.py
grep -n "from src\.\|import src\." aq_memory/extractor.py | head -20
```

- [ ] **Step 4: Rewrite imports + class hierarchy in `aq_memory/plugin.py`**

```bash
sed -i 's|src\.plugins\.internal\.memory\.|aq_memory.|g' aq_memory/plugin.py
sed -i 's|from src\.plugins\.base import InternalPlugin|from src.plugins.base import Plugin, PluginContext, PluginPermission|' aq_memory/plugin.py
sed -i 's|class MemoryPlugin(InternalPlugin):|class MemoryPlugin(Plugin):|' aq_memory/plugin.py
```

Then open the file and:
1. Find / delete any `_internal = True` class attribute (was the marker for internal-plugin discovery).
2. In `initialize`, locate the existing `ctx.register_service("memory", self._service)` call (added in Task 4 step 3). It should already be there from the rename / wiring done in Task 4 — confirm.
3. Add the facts watcher wiring (moved from orchestrator) — in `initialize`, after `register_service`:

```python
# Wire facts.md watcher handlers (was in orchestrator/core.py).
if self._service is not None and getattr(self._service, "available", False):
    try:
        watcher = ctx.get_service("vault_watcher")
    except (ValueError, PermissionError):
        watcher = None
    if watcher is not None:
        from src.facts_handler import register_facts_handlers
        register_facts_handlers(watcher, service=self._service)
        ctx.logger.info("Wired memory service to facts.md watcher handlers")
```

- [ ] **Step 5: Update `aq_memory/__init__.py` re-exports**

If the original `src/plugins/internal/memory/__init__.py` re-exported `AGENT_TOOLS, TOOL_CATEGORY, TOOL_DEFINITIONS, MemoryPlugin` (it did, per the early exploration), do the same:

```python
"""Memory plugin for agent-queue.

Self-contained memory subsystem — service layer, background extractor,
and all command handlers live in this package.
"""

from aq_memory.plugin import (  # noqa: F401
    AGENT_TOOLS,
    TOOL_CATEGORY,
    TOOL_DEFINITIONS,
    MemoryPlugin,
)

__all__ = [
    "AGENT_TOOLS",
    "TOOL_CATEGORY",
    "TOOL_DEFINITIONS",
    "MemoryPlugin",
]
```

(Drop `V2_ONLY_TOOLS` — already removed in Task 1 step 8.)

- [ ] **Step 6: Install the new plugin editable**

```bash
cd /mnt/d/Dev/aq/aq-memory
pip install -e .
```

Expected: package builds and installs. The `memsearch[ollama] @ file:packages/memsearch` dep will FAIL because `packages/memsearch/` does not exist yet. That's expected — it lands in Task 7.

For now, comment out the dep temporarily:

```bash
# In pyproject.toml, comment the memsearch line:
# "memsearch[ollama] @ file:packages/memsearch",
```

Reinstall, confirm it works:

```bash
pip install -e . --no-deps
python -c "from aq_memory import MemoryPlugin; print(MemoryPlugin.__module__)"
```

Expected: `aq_memory.plugin`.

- [ ] **Step 7: Commit aq-memory changes**

```bash
cd /mnt/d/Dev/aq/aq-memory
git add -A
git commit -m "Move memory plugin source from agent-queue2"
```

---

## Task 7: Move `memsearch` Package into aq-memory

**Goal:** Move the `packages/memsearch/` directory from agent-queue2 into aq-memory, restore the `memsearch` dep in `aq-memory/pyproject.toml`, and verify the plugin still imports.

- [ ] **Step 1: Move the package**

```bash
mv /home/jkern/dev/agent-queue2/packages/memsearch /mnt/d/Dev/aq/aq-memory/packages/memsearch
```

(Note: `packages/` exists in aq-memory because the move creates it. If `mv` complains, `mkdir -p /mnt/d/Dev/aq/aq-memory/packages` first then move.)

- [ ] **Step 2: Restore the memsearch dep in `aq-memory/pyproject.toml`**

Uncomment:

```toml
dependencies = [
    "memsearch[ollama] @ file:packages/memsearch",
]
```

- [ ] **Step 3: Reinstall both packages**

```bash
cd /mnt/d/Dev/aq/aq-memory
pip install -e .
```

Expected: both `memsearch` and `aq-memory` install. If `memsearch` build fails, check `packages/memsearch/pyproject.toml` for path-relative assumptions that may have broken in the move.

- [ ] **Step 4: Verify import**

```bash
python -c "import memsearch; print(memsearch.__file__)"
python -c "from aq_memory import MemoryPlugin; print(MemoryPlugin)"
```

Expected: both succeed and print paths inside `/mnt/d/Dev/aq/aq-memory`.

- [ ] **Step 5: Commit aq-memory changes**

```bash
cd /mnt/d/Dev/aq/aq-memory
git add -A
git commit -m "Vendor memsearch package"
```

---

## Task 8: Move Memory Tests to aq-memory + Strengthen Contract Test in agent-queue2

**Goal:** Move 10 memory unit tests into `aq-memory/tests/`, leave 2 contract tests in agent-queue2, gate `tests/test_mcp_memory_roundtrip.py` with `pytest.importorskip("aq_memory")`.

**Files moved (from `agent-queue2/tests/` to `aq-memory/tests/`):**

| Source (agent-queue2) | Destination (aq-memory) |
|---|---|
| `tests/test_memory_service.py` (renamed in Task 1) | `tests/test_memory_service.py` |
| `tests/test_memory_extractor.py` | `tests/test_memory_extractor.py` |
| `tests/test_memory_save.py` | `tests/test_memory_save.py` |
| `tests/test_memory_promote.py` | `tests/test_memory_promote.py` |
| `tests/test_memory_consolidation_playbook.py` | `tests/test_memory_consolidation_playbook.py` |
| `tests/test_memory_consolidation_tools.py` | `tests/test_memory_consolidation_tools.py` |
| `tests/test_memory_audit_trail.py` | `tests/test_memory_audit_trail.py` |
| `tests/test_memory_health.py` | `tests/test_memory_health.py` |
| `tests/test_memory_contradiction.py` | `tests/test_memory_contradiction.py` |
| `tests/test_memory_scope_alias.py` | `tests/test_memory_scope_alias.py` |
| `tests/test_stale_memory_detection.py` | `tests/test_stale_memory_detection.py` |

**Stays in agent-queue2** (with import-skip gating):
- `tests/test_mcp_memory_roundtrip.py` — gated with `pytest.importorskip("aq_memory")`
- `tests/test_memory_extension_points.py` — already pure-core (uses stub plugin), no gating needed

- [ ] **Step 1: Move test files**

```bash
cd /home/jkern/dev/agent-queue2
mv tests/test_memory_service.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_extractor.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_save.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_promote.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_consolidation_playbook.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_consolidation_tools.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_audit_trail.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_health.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_contradiction.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_memory_scope_alias.py /mnt/d/Dev/aq/aq-memory/tests/
mv tests/test_stale_memory_detection.py /mnt/d/Dev/aq/aq-memory/tests/
```

- [ ] **Step 2: Update imports in moved tests**

For each moved test, run a sed pass:

```bash
cd /mnt/d/Dev/aq/aq-memory/tests
for f in test_memory_*.py test_stale_memory_detection.py; do
  sed -i 's|src\.plugins\.internal\.memory\.|aq_memory.|g' "$f"
done
```

Then check for stragglers (e.g. relative path imports of `tests.conftest` or other agent-queue2 test helpers):

```bash
grep -rn "from src\." tests/
grep -rn "from tests\." tests/
```

Anything that reaches into `tests.<something>` from agent-queue2 needs to either:
- be ported into `aq-memory/tests/conftest.py` (preferred for plugin-specific helpers), OR
- be inlined in the test file.

Cross-cutting fixtures that exist in `agent-queue2/tests/conftest.py` (like `db`, `event_bus`, `app_config`) need an aq-memory equivalent. Create `/mnt/d/Dev/aq/aq-memory/tests/conftest.py` and copy in (or re-import from agent-queue2 if practical) the fixtures the moved tests use. Inspect each test file's `pytest.fixture` consumption to compile the list.

- [ ] **Step 3: Run aq-memory tests**

```bash
cd /mnt/d/Dev/aq/aq-memory
pytest tests/ -n auto -q 2>&1 | tail -30
```

Expected: tests pass (modulo any that need fixture rework — fix those iteratively).

- [ ] **Step 4: Gate the MCP roundtrip test in agent-queue2**

Open `agent-queue2/tests/test_mcp_memory_roundtrip.py` and add at the top (after the docstring, before imports of `aq_memory`):

```python
import pytest

pytest.importorskip("aq_memory")
```

This skips the whole module when the plugin isn't installed — matching the spec §7 requirement that agent-queue2's CI passes without aq-memory.

- [ ] **Step 5: Run agent-queue2 tests**

```bash
cd /home/jkern/dev/agent-queue2
pytest tests/ -n auto -q 2>&1 | tail -20
```

Expected: passes. With aq-memory installed (from Task 7), the gated test runs normally. Without aq-memory, it skips.

- [ ] **Step 6: Commit both repos**

```bash
cd /mnt/d/Dev/aq/aq-memory
git add -A
git commit -m "Add memory unit tests (moved from agent-queue2)"

cd /home/jkern/dev/agent-queue2
git add tests/
git commit -m "Move memory unit tests to aq-memory; gate MCP roundtrip test"
```

---

## Task 9: Verify Identical End-to-End Behavior with Plugin Installed

**Goal:** With aq-memory installed (from Task 7) but the internal plugin still in place at `src/plugins/internal/memory/`, the system has TWO memory plugins. Verify which wins, then prove the external plugin alone is sufficient.

- [ ] **Step 1: Disable the internal plugin's auto-discovery temporarily**

The internal plugin discovery scans `src/plugins/internal/`. Easiest disable: rename its `__init__.py` so `pkgutil.iter_modules` skips it.

```bash
cd /home/jkern/dev/agent-queue2
mv src/plugins/internal/memory/__init__.py src/plugins/internal/memory/__init__.py.disabled
```

(This is a temporary shim — Task 10 deletes the directory entirely.)

- [ ] **Step 2: Smoke test the app with only the external plugin**

```bash
./run.sh start
# Watch logs for:
#   "Loaded plugin: memory" (external)
#   "Wired memory service to facts.md watcher handlers"
# Issue a Discord command that uses memory_recall.
./run.sh stop
```

Expected: external aq-memory loads cleanly, registers `"memory"` service, wires facts watcher, all memory tools work.

- [ ] **Step 3: Run the full agent-queue2 test suite**

```bash
pytest tests/ -n auto -q 2>&1 | tail -20
```

Expected: same pass count as before, including the gated `test_mcp_memory_roundtrip.py` which now runs against the external plugin.

- [ ] **Step 4: Re-enable internal plugin to confirm rollback works**

```bash
mv src/plugins/internal/memory/__init__.py.disabled src/plugins/internal/memory/__init__.py
```

Verify both plugins coexist (one wins, one logs a "service overridden" warning per Task 2 step 4 implementation):

```bash
./run.sh start
# Look for: "Plugin 'memory' overrides service 'memory' previously registered by 'memory'"
# (or similar — the warning is informational; one plugin wins.)
./run.sh stop
```

This is a sanity check for the override warning. Re-disable the internal plugin again before continuing:

```bash
mv src/plugins/internal/memory/__init__.py src/plugins/internal/memory/__init__.py.disabled
```

- [ ] **Step 5: No commit**

This task is verification only. If steps 1-4 pass, proceed to Task 10. If anything fails, fix and re-run.

---

## Task 10: Delete Internal Plugin and `packages/memsearch/` from agent-queue2

**Goal:** With the external plugin proven to work, remove the internal copy entirely.

- [ ] **Step 1: Delete the internal memory plugin directory**

```bash
cd /home/jkern/dev/agent-queue2
git rm -r src/plugins/internal/memory/
```

(The disabled `__init__.py.disabled` from Task 9 step 1 is included in the removal.)

- [ ] **Step 2: Drop `memsearch` dep from agent-queue2 `pyproject.toml`**

Edit `pyproject.toml`:

```toml
# Remove this line:
"memsearch[ollama] @ file:packages/memsearch",
```

Also remove or update the comment at line 49 / 94-95 that references memsearch (these were noise comments; check whether they still make sense after removing the dep).

- [ ] **Step 3: Reinstall agent-queue2 to pick up dep change**

```bash
pip install -e ".[dev,cli]"
```

Expected: install succeeds. memsearch may still be present in the venv from aq-memory's editable install — that's fine.

- [ ] **Step 4: Run full test suite**

```bash
pytest tests/ -n auto -q 2>&1 | tail -20
```

Expected: same pass count. The internal plugin is gone; tests pass against the external plugin (which is installed via Task 7).

- [ ] **Step 5: Smoke test the app**

```bash
./run.sh start
# Verify: only "Loaded plugin: memory" appears once in logs (no internal plugin discovery noise).
./run.sh stop
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
Remove internal memory plugin and vendored memsearch

Memory functionality now lives entirely in the external aq-memory
plugin at /mnt/d/Dev/aq/aq-memory.  agent-queue2 no longer ships a
memory implementation; users opt in via 'aq plugin install'.

Spec §5.5, §5.6.
EOF
)"
```

---

## Task 11: No-Memory Acceptance Test (spec §8)

**Goal:** Prove agent-queue2 runs end-to-end with NO memory plugin installed. This is the headline acceptance criterion.

- [ ] **Step 1: Uninstall aq-memory from the venv**

```bash
pip uninstall -y aq-memory memsearch
```

- [ ] **Step 2: Verify nothing imports `aq_memory` in agent-queue2**

```bash
cd /home/jkern/dev/agent-queue2
grep -rn "import aq_memory\|from aq_memory" src/ tests/ | grep -v "tests/test_mcp_memory_roundtrip.py"
```

Expected: zero results (the only import-site is the gated MCP test, which uses `importorskip`).

- [ ] **Step 3: Run full test suite without memory installed**

```bash
pytest tests/ -n auto -q 2>&1 | tail -30
```

Expected:
- All non-memory tests PASS.
- `tests/test_mcp_memory_roundtrip.py` shows as SKIPPED.
- `tests/test_memory_extension_points.py` PASSES (it uses an inline stub plugin and exercises the absent-cleanly case).
- No collection errors mentioning `memsearch` or `aq_memory`.

- [ ] **Step 4: Smoke test the app with no memory**

```bash
./run.sh start
# Logs should show:
#   - No "Loaded plugin: memory"
#   - No errors about missing memory
#   - Vault watcher loads but has no memory-side handlers wired
# Issue a small task that doesn't need memory (e.g., a status command).
./run.sh stop
```

Expected: clean boot, no memory tools in agent tool lists, no errors. L1 facts in supervisor prompt is empty.

- [ ] **Step 5: Reinstall aq-memory and verify "back on" path**

```bash
pip install -e /mnt/d/Dev/aq/aq-memory
./run.sh start
# Logs should now show:
#   - "Loaded plugin: memory"
#   - "Wired memory service to facts.md watcher handlers"
# Memory tools appear in agent tool lists.
./run.sh stop
```

Expected: feature returns when plugin is reinstalled. This is the proof that "remove + reinstall" is a complete cycle.

- [ ] **Step 6: No commit (verification only)**

If any step fails, fix the underlying issue (likely a stray import somewhere). Repeat until clean.

---

## Task 12: Add Optional Memory Install to `setup.sh` (spec §5.7)

**Goal:** New users who run `setup.sh` get an interactive prompt to install aq-memory. Default is no.

**Files:**
- Modify: `/home/jkern/dev/agent-queue2/setup.sh`

- [ ] **Step 1: Read current `setup.sh`**

```bash
cat /home/jkern/dev/agent-queue2/setup.sh
```

Find a sensible place to add the prompt (after the agent-queue2 install completes, before the script exits).

- [ ] **Step 2: Add the interactive + flag-driven prompt**

Append (or insert at the right spot):

```bash
# --- Optional memory plugin ---

INSTALL_MEMORY=0
for arg in "$@"; do
    case "$arg" in
        --with-memory) INSTALL_MEMORY=1 ;;
        --no-memory)   INSTALL_MEMORY=0 ;;
    esac
done

if [ "$INSTALL_MEMORY" -eq 0 ] && [ -t 0 ]; then
    read -p "Install aq-memory plugin (Milvus-backed memory)? (y/N) " yn
    case "$yn" in
        [Yy]*) INSTALL_MEMORY=1 ;;
    esac
fi

if [ "$INSTALL_MEMORY" -eq 1 ]; then
    AQ_MEMORY_PATH="${AQ_MEMORY_PATH:-/mnt/d/Dev/aq/aq-memory}"
    if [ -d "$AQ_MEMORY_PATH" ]; then
        echo "Installing aq-memory from $AQ_MEMORY_PATH..."
        aq plugin install "$AQ_MEMORY_PATH"
    else
        echo "aq-memory not found at $AQ_MEMORY_PATH — skipping."
        echo "  Set AQ_MEMORY_PATH=<dir> or run: aq plugin install <git-or-path>"
    fi
fi
```

The `[ -t 0 ]` check skips the prompt when stdin is not a tty (CI / scripted installs). `--with-memory` / `--no-memory` flags override the default for non-interactive use.

- [ ] **Step 3: Test the prompt**

```bash
# Interactive (will prompt):
./setup.sh

# Non-interactive yes:
./setup.sh --with-memory

# Non-interactive no (the default; explicit flag for clarity):
./setup.sh --no-memory
```

Expected: each behaves as documented.

- [ ] **Step 4: Commit**

```bash
git add setup.sh
git commit -m "$(cat <<'EOF'
Add optional aq-memory install prompt to setup.sh

Default is no — agent-queue2 ships memory-free.  Interactive runs see a
y/N prompt; CI / scripted installs use --with-memory / --no-memory.
Path defaults to /mnt/d/Dev/aq/aq-memory and can be overridden via
AQ_MEMORY_PATH env var.

Spec §5.7.
EOF
)"
```

---

## Task 13: Documentation Pass (spec §9 step 14)

**Goal:** Update affected docs to reflect the post-extraction reality.

**Files:**
- Modify: `docs/specs/design/memory-plugin.md` — note the plugin is now external
- Modify: `docs/specs/plugin-system.md` — update Phase 5 status
- Modify: `CLAUDE.md` — update plugin list (memory_v2 → external)
- Modify: `profile.md` — same
- Modify: `docs/specs/design/roadmap.md` — mark roadmap item complete (if relevant)

- [ ] **Step 1: Update `docs/specs/design/memory-plugin.md`**

In §1 (Overview), change "self-contained internal plugin" to "self-contained external plugin shipped as a separate repo at https://github.com/<owner>/aq-memory (or local: /mnt/d/Dev/aq/aq-memory)."

In §4 (Why a Plugin), confirm the bullets still apply. Add a note about the §5.0 V2-suffix rename being completed.

- [ ] **Step 2: Update `docs/specs/plugin-system.md`**

Under "Phase 5" (line ~652), mark memory plugin extraction as complete:

```markdown
### Phase 5: First Plugin Extraction
- ✅ Memory plugin extracted to external repo (aq-memory)
- (File Watcher extraction: still planned)
- ✅ Plugin authoring guide via aq-memory's CLAUDE.md
- (Plugin template repository: still planned)
```

In §10 (Candidates for Extraction), strike Memory from the candidates list (it's done).

- [ ] **Step 3: Update `CLAUDE.md`**

Find the "Internal plugins" line:

```markdown
- **Internal plugins:** `src/plugins/internal/` (aq-files, aq-git, aq-memory-v2, aq-notes, aq-vibecop)
```

Change to:

```markdown
- **Internal plugins:** `src/plugins/internal/` (aq-files, aq-git, aq-notes, aq-vibecop)
- **External plugins available:** aq-memory (https://github.com/... or local /mnt/d/Dev/aq/aq-memory)
```

- [ ] **Step 4: Update `profile.md`**

Same pattern — anywhere it lists memory_v2 as an internal plugin, update.

- [ ] **Step 5: Update roadmap if applicable**

Search for "memory plugin", "extraction", "external plugin" in roadmap and mark related items done.

- [ ] **Step 6: Commit**

```bash
git add docs/ CLAUDE.md profile.md
git commit -m "Update docs: memory plugin is now external (aq-memory)"
```

---

## Final Push

After Task 13:

```bash
cd /home/jkern/dev/agent-queue2
git push -u origin extract-memory-plugin

cd /mnt/d/Dev/aq/aq-memory
git remote add origin <github-url-when-created>
git push -u origin main
```

(The aq-memory remote URL is created externally — outside this plan.)

Open a PR for `extract-memory-plugin` against `main` in agent-queue2. The PR description should:
- Link to the spec (`docs/superpowers/specs/2026-04-25-aq-memory-extraction-design.md`).
- Mention the companion repo (`aq-memory`).
- Note the §8 acceptance test was passed.

---

## Self-Review Notes

**Spec coverage check:**

- §1 Goal — Tasks 5-11 collectively achieve the extraction.
- §2 Non-goals — respected (no rewrite of internals; same tool surface; no PyPI publish).
- §3 Constraints — `MemoryServiceProtocol` is renamed in Task 1 and used as the contract throughout. Five call sites switched in Task 4.
- §4 Target Architecture — directory layouts realized in Tasks 5-7, communication boundary realized in Task 4.
- §5.0 Rename — Task 1.
- §5.1 / §5.2 `register_service` / `get_service` — Task 2.
- §5.3 Five call site changes — Task 4 steps 4-7.
- §5.4 Move facts watcher wiring — Task 6 step 4.
- §5.5 Delete `src/plugins/internal/memory/` — Task 10 step 1.
- §5.6 `pyproject.toml` updates — Task 10 step 2.
- §5.7 Setup script prompt — Task 12.
- §6.1 `pyproject.toml` for aq-memory — Task 5 step 2.
- §6.2 Plugin class — Task 6 step 4.
- §6.3 PluginContext additions — Task 2 (`register_service`), Task 3 (allowlist + `vault_watcher`).
- §7 Tests — Task 8.
- §8 Acceptance test — Task 11.
- §9 Migration order — followed by task numbering.
- §10 Risks — addressed: external get_service in Task 3, async event subscription confirmed in Task 4 (`ctx.subscribe` exists), MCP server registration validated by smoke test in Task 9, DB cleanup left intentional, memsearch local install verified in Task 7.
- §11 Out of scope — respected.

**Placeholder scan:** None. All steps contain concrete code or commands. The setup.sh edit references `AQ_MEMORY_PATH` which has a default. The PR push step references "github-url-when-created" which is acknowledged as out-of-plan.

**Type consistency:** `MemoryServiceProtocol`, `MemoryService`, `MemoryPlugin`, `VaultWatcherService`, `register_service`, `get_service`, `register_plugin_service` are used consistently across tasks.
