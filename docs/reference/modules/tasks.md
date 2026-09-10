# Task modules

This catalog covers the production modules owned by the [tasks concept page](../../concepts/tasks.md). Paths are source links; tests are focused entry points rather than an exhaustive test index.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/deliverables.py](../../../src/deliverables.py) | Normalizes declared deliverables and evaluates close-time local evidence. | [Tasks](../../concepts/tasks.md) | `tests/test_deliverables.py` |
| [src/explain.py](../../../src/explain.py) | Produces structured capacity and readiness explanations for a task. | [Tasks](../../concepts/tasks.md) | `tests/test_explain.py` |
| [src/review_keys.py](../../../src/review_keys.py) | Recognizes tasks whose work product is a review verdict. | [Tasks](../../concepts/tasks.md) | Task lifecycle support; see task command tests. |
| [src/state_machine.py](../../../src/state_machine.py) | Defines intended status transitions and validates blocking dependency cycles. | [Tasks](../../concepts/tasks.md) | `tests/test_state_machine.py` |
| [src/task_names.py](../../../src/task_names.py) | Mints memorable root and dotted child task identifiers with depth limits. | [Tasks](../../concepts/tasks.md) | `tests/test_hierarchy_ids.py` |
| [src/task_summary.py](../../../src/task_summary.py) | Renders and writes a completion summary note to the vault. | [Tasks](../../concepts/tasks.md) | Completion and archive tests cover its callers. |
| [src/task_graph/__init__.py](../../../src/task_graph/__init__.py) | Exposes the task-graph package's public graph model surface. | [Tasks](../../concepts/tasks.md) | `tests/test_task_graph.py` |
| [src/task_graph/models.py](../../../src/task_graph/models.py) | Defines the in-memory graph document, nodes, dependencies, contexts and parse findings. | [Tasks](../../concepts/tasks.md) | `tests/test_task_graph.py` |
| [src/task_graph/parser.py](../../../src/task_graph/parser.py) | Parses JSON/YAML graph documents and extracts fenced graphs from specs. | [Tasks](../../concepts/tasks.md) | `tests/test_task_graph.py` |
| [src/task_graph/validator.py](../../../src/task_graph/validator.py) | Validates graph structure, dependencies, variables and vault-contained spec references. | [Tasks](../../concepts/tasks.md) | `tests/test_task_graph.py`, `tests/test_hierarchy_graph_creator.py` |
| [src/task_graph/creator.py](../../../src/task_graph/creator.py) | Plans and atomically persists a validated graph, including hierarchy and provenance. | [Tasks](../../concepts/tasks.md) | `tests/test_create_task_graph_command.py`, `tests/test_hierarchy_graph_creator.py` |
| [src/task_graph/formulas.py](../../../src/task_graph/formulas.py) | Loads, resolves, validates and watches reusable vault-backed graph formulas. | [Tasks](../../concepts/tasks.md) | `tests/test_formulas_parse.py`, `tests/test_formulas_resolve.py`, `tests/test_formula_commands.py` |
| [src/task_graph/layout/__init__.py](../../../src/task_graph/layout/__init__.py) | Re-exports the stable layout data types used by server-side graph layout. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/` |
| [src/task_graph/layout/model.py](../../../src/task_graph/layout/model.py) | Defines snapshots, persisted rows, scopes, translations and write sets for layout. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/test_layout_driver.py` |
| [src/task_graph/layout/constants.py](../../../src/task_graph/layout/constants.py) | Centralizes deterministic geometry, ranking and work-budget constants. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/` |
| [src/task_graph/layout/order_key.py](../../../src/task_graph/layout/order_key.py) | Generates sortable fractional order keys between neighboring layout items. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/test_order_key.py` |
| [src/task_graph/layout/layering.py](../../../src/task_graph/layout/layering.py) | Breaks layout-only cycles and computes minimum dependency ranks. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/test_layering.py` |
| [src/task_graph/layout/flow.py](../../../src/task_graph/layout/flow.py) | Flows ranked siblings into wrapped coordinates and spatial cells. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/test_flow.py` |
| [src/task_graph/layout/cost.py](../../../src/task_graph/layout/cost.py) | Scores crossings, span, wrapping and rank slack for layout optimization. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/test_cost.py` |
| [src/task_graph/layout/engine.py](../../../src/task_graph/layout/engine.py) | Lays out one container incrementally, on resize, or during a full tidy. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/test_engine_incremental.py`, `tests/task_graph/layout/test_engine_tidy.py` |
| [src/task_graph/layout/driver.py](../../../src/task_graph/layout/driver.py) | Builds project write sets and persists/reconciles full or dirty incremental layouts. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/test_layout_driver.py`, `tests/task_graph/test_layout_dirty_marks.py` |
| [src/task_graph/layout/compaction.py](../../../src/task_graph/layout/compaction.py) | Derives a viewer's compact geometry from persisted expanded rows and collapsed containers. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/test_compaction.py` |
| [src/task_graph/layout/view.py](../../../src/task_graph/layout/view.py) | Resolves visible nodes, remaps hidden edges and docks worker indicators for graph views. | [Tasks](../../concepts/tasks.md) | `tests/task_graph/layout/test_view.py` |
