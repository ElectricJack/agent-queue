# Agent Queue documentation overhaul

AQ epic: `solid-grove`; children `solid-grove.1` through `solid-grove.24`. Created in project `agent-queue` with 69 dependency edges.

Write for a person who has never used AQ. Explain the user workflow first, then progressively expose internals. This plan creates documentation work; it does not claim the documentation is already updated.

## Intended navigation

README → Start here → Installation and first task → Core concepts → How-to guides → Reference (CLI/API/configuration/modules) → Contributing → Historical designs/reports.

Each module must be findable by source path and by its purpose. Component guides may group private helpers, but the module catalog must explicitly map every production module to an explanation. Generated clients link to their authoritative schema/resource-family reference.

## Execution and ownership

Foundation establishes the complete inventory and page ownership. Subsystem tickets then proceed independently. Reference automation and legacy reconciliation follow their inputs; the final ticket assembles navigation and verifies coverage and a disposable newcomer walkthrough. Shared indexes are owned by foundation/final assembly; each subsystem writes its own coverage shard. All tickets use standard-high and omit profile pins.

## Child tasks

1. **Define beginner documentation hierarchy and complete module coverage inventory** (`foundation`) — depends on: none.
2. **Write installation and first-task tutorial for a completely new user** (`quickstart`) — depends on: foundation.
3. **Rewrite GitHub README and newcomer overview around current behavior** (`readme`) — depends on: foundation, quickstart.
4. **Document system architecture, startup and service boundaries** (`architecture`) — depends on: foundation.
5. **Document task lifecycle, epics, dependencies, formulas and graph layout** (`tasks`) — depends on: foundation.
6. **Document scheduling, worker pools and resource limits** (`scheduler`) — depends on: foundation.
7. **Document agents, profiles, intelligence classes and assignment routing** (`routing`) — depends on: foundation.
8. **Document worker sessions, harnesses, terminals and claim recovery** (`sessions`) — depends on: foundation.
9. **Document projects, Git branches, workspaces and worktree slots** (`workspaces`) — depends on: foundation.
10. **Document development integration and optional strict modes accurately** (`integration`) — depends on: foundation.
11. **Document Playbooks V2, events, gates and authoring** (`playbooks`) — depends on: foundation.
12. **Document the full AQ CLI, command layer, contracts and agent-facing tools** (`cli`) — depends on: foundation.
13. **Document REST, WebSocket and generated Python/TypeScript clients** (`api`) — depends on: foundation.
14. **Document the dashboard for users and its frontend modules for contributors** (`dashboard`) — depends on: foundation.
15. **Document database schema, query modules, migrations and data lifecycle** (`database`) — depends on: foundation.
16. **Document configuration, vault, knowledge files and prompt assembly** (`vault`) — depends on: foundation.
17. **Document LLM providers, usage accounting, tokens and budgets** (`providers`) — depends on: foundation.
18. **Document plugins, MCP and extension development** (`plugins`) — depends on: foundation.
19. **Document messaging, Discord digests and escalation workflows** (`communications`) — depends on: foundation.
20. **Document diagnostics, observability, recovery and day-to-day operation** (`operations`) — depends on: foundation.
21. **Document contributor setup, testing, builds and release tooling** (`contributing`) — depends on: foundation.
22. **Build maintainable configuration, command and module reference checks** (`reference`) — depends on: foundation, cli, api, vault.
23. **Reconcile all existing documentation and clearly separate historical material** (`legacy`) — depends on: foundation, quickstart, readme, architecture, tasks, scheduler, routing, sessions, workspaces, integration, playbooks, cli, api, dashboard, database, vault, providers, plugins, communications, operations, contributing.
24. **Verify complete module coverage and publish the unified GitHub documentation view** (`acceptance`) — depends on: foundation, quickstart, readme, architecture, tasks, scheduler, routing, sessions, workspaces, integration, playbooks, cli, api, dashboard, database, vault, providers, plugins, communications, operations, contributing, reference, legacy.

## Acceptance

Every tracked production module and existing documentation file receives an explicit coverage/disposition entry. Guides reflect current main and distinguish shipped defaults, configured policy, optional compatibility and historical plans. Commands and paths are checked locally; no full application test run is required for documentation-only changes. GitHub-native Markdown and relative links are the required publication format; introducing a separate documentation hosting platform is not part of this epic.

See [task graph](tasks.graph.json) for each scope and acceptance checklist.

## Foundation outputs

The `foundation` ticket has landed. Everything below is the contract the other
23 tickets work against:

| Artefact | What it is |
|---|---|
| [`docs/README.md`](../../README.md) | The documentation home: navigation and beginner reading order. |
| [`docs/documentation-map.md`](../../documentation-map.md) | The tree, the page ownership table and the shard rules. **Read this before writing a page.** |
| [`docs/reference/glossary.md`](../../reference/glossary.md) | Shared vocabulary. |
| [`docs/contributing/documentation-style.md`](../../contributing/documentation-style.md) | Page shape, accuracy rules and the runnable-example rules. |
| [`docs/reference/modules/README.md`](../../reference/modules/README.md) | Module catalog index and the shard entry format. |
| [`module-ownership.json`](module-ownership.json) | Every tracked path → owning shard, component page and category. |
| [`module-inventory.json`](module-inventory.json) | The refreshed inventory, regenerated against implementation `HEAD`. |
| [`known-inaccuracies.md`](known-inaccuracies.md) | Where existing pages contradict current behaviour, with evidence. |
| [`refresh_inventory.py`](refresh_inventory.py) | Regenerates and checks the two JSON artefacts. |

Tickets run the coverage half; the freshness half is this shard's gate, because
the two artefacts are foundation-owned and a per-branch rewrite of a
3,700-entry JSON conflicts with every other in-flight ticket:

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check            # every ticket
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check-artefacts  # this shard
python3 docs/plans/documentation-overhaul/refresh_inventory.py                    # this shard
```
