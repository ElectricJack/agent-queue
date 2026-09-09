# Module catalog — system architecture

This is the `architecture` shard of the [module catalog](README.md). It maps
the daemon composition root, service seam, compatibility boundary and runtime
registry to [System architecture](../../concepts/architecture.md). The six
modules below are the complete production scope assigned to this shard.

## Daemon composition and compatibility

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/__init__.py`](../../../src/__init__.py) | Applies the early compatibility import when the `src` package is imported. | [System architecture](../../concepts/architecture.md) | Ensures the narrow dependency shim precedes memory-related imports; [`tests/test_compat.py`](../../../tests/test_compat.py). |
| [`src/_compat.py`](../../../src/_compat.py) | Installs idempotent Python/setuptools compatibility shims for legacy `pkg_resources` consumers. | [System architecture](../../concepts/architecture.md) | Supplies `pkgutil.ImpImporter` and, only when unavailable, a minimal metadata-backed `pkg_resources` module; [`tests/test_compat.py`](../../../tests/test_compat.py). |
| [`src/main.py`](../../../src/main.py) | Composes the daemon, controls startup/shutdown, runs the scheduler cadence and wires optional messaging/MCP/delivery services. | [System architecture](../../concepts/architecture.md) | The process composition root, not the scheduler's business-logic owner; [`tests/test_main_lifecycle.py`](../../../tests/test_main_lifecycle.py), [`tests/test_health_platform.py`](../../../tests/test_health_platform.py). |

## Service and runtime extension seams

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/services/__init__.py`](../../../src/services/__init__.py) | Declares the service-layer seam for stateful, long-lived cross-module workflows. | [System architecture](../../concepts/architecture.md) | Imports the compatibility shim first; concrete services live in their owning subsystem packages and are catalogued there. |
| [`src/runtimes/__init__.py`](../../../src/runtimes/__init__.py) | Exposes `RuntimeRegistry` and constructs the intentionally empty production registry. | [System architecture](../../concepts/architecture.md) | A test/plugin injection seam, not a list of shipped workers; [`tests/test_runtimes_registry.py`](../../../tests/test_runtimes_registry.py). |
| [`src/runtimes/base.py`](../../../src/runtimes/base.py) | Defines the optional in-process `Runtime` lifecycle contract and capability vocabulary. | [System architecture](../../concepts/architecture.md) | No in-tree class implements it; normal execution uses sessions and harnesses. [`tests/test_runtimes_registry.py`](../../../tests/test_runtimes_registry.py). |

## Neighbouring modules

These paths participate in the architecture but are catalogued by the
subsystem that owns their detailed behaviour:

| Module | Owning shard | Why it is nearby |
|---|---|---|
| [`src/orchestrator/core.py`](../../../src/orchestrator/core.py) | scheduler | Owns initialization, scheduling and orderly subsystem shutdown that `src/main.py` calls. |
| [`src/commands/handler.py`](../../../src/commands/handler.py) | cli | Is the unified command boundary wired by the daemon. |
| [`src/api/app.py`](../../../src/api/app.py) | api | Builds the FastAPI surface around the daemon's shared orchestrator and handler. |
| [`src/sessions/`](../../../src/sessions/) | sessions | Builds, launches, observes and recovers the worker-session path. |
| [`src/integration/`](../../../src/integration/) | integration | Owns configured publication and integration lifecycle services. |
| [`src/messaging/`](../../../src/messaging/) | communications | Defines the adapter port selected by daemon composition. |

## Focused verification

```bash
aq test tests/test_main_lifecycle.py tests/test_health_platform.py tests/test_runtimes_registry.py tests/test_compat.py
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```

The second command checks documentation ownership only. It does not regenerate
the foundation-owned inventory artefacts.
