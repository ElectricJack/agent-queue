# Module catalog

Every module that ships in the running system, what it does, and the page that
explains it. If you have a source path and want the prose, start here. If you
have a concept and want the source, start at the
[documentation home](../../README.md).

The catalog is split into **shards**, one per subject area, so that the people
documenting different subsystems never edit the same file. Each shard lists its
own modules; this page is only the index.

> **Status.** The overhaul is in progress. Shard file names become links as
> the shards land; the rest are not written yet. The module counts are already fixed
> by the
> [coverage manifest](../../plans/documentation-overhaul/module-ownership.json),
> so you can see how much of each area is outstanding.

## Shards

| Shard | Ticket | Production modules | Generated files | Component pages |
|---|---|---|---|---|
| `architecture.md` | `architecture` | 6 | — | `concepts/architecture.md` |
| `tasks.md` | `tasks` | 23 | — | `concepts/tasks.md` |
| `scheduler.md` | `scheduler` | 25 | — | `concepts/scheduling.md`, `guides/resource-gating.md` |
| `routing.md` | `routing` | 23 | — | `concepts/agents-and-routing.md` |
| `sessions.md` | `sessions` | 30 | — | `concepts/sessions.md` |
| `workspaces.md` | `workspaces` | 22 | — | `concepts/projects-and-workspaces.md` |
| `integration.md` | `integration` | 29 | — | `concepts/integration.md` |
| `playbooks.md` | `playbooks` | 42 | 5 | `concepts/playbooks.md` |
| `cli.md` | `cli` | 94 | — | `reference/cli/README.md`, `reference/cli/agent-tools.md`, `reference/cli/contracts.md`, `reference/cli/prime.md` |
| [`api.md`](api.md) | `api` | 51 | 1442 | [`reference/api/README.md`](../api/README.md), [`conventions.md`](../api/conventions.md), [`events.md`](../api/events.md), [`models.md`](../api/models.md), [`python-client.md`](../api/python-client.md), [`typescript-client.md`](../api/typescript-client.md) |
| `dashboard.md` | `dashboard` | 258 | — | `guides/dashboard.md` |
| `database.md` | `database` | 61 | — | `reference/database/README.md`, `reference/database/queries.md` |
| `vault.md` | `vault` | 19 | — | `concepts/configuration-and-vault.md`, `reference/configuration.md` |
| `providers.md` | `providers` | 22 | — | `concepts/providers.md` |
| `plugins.md` | `plugins` | 24 | — | `guides/plugins-and-mcp.md` |
| `communications.md` | `communications` | 38 | — | `concepts/messaging.md` |
| `operations.md` | `operations` | 24 | — | `guides/operations.md` |
| `contributing.md` | `contributing` | 0 | — | — |

`contributing` carries no production modules: it documents the 81 supporting
files — scripts, CI workflows, packaging and build configuration — and the test
layout, by purpose rather than symbol by symbol.

## What a catalog entry must contain

Each shard is a table with one row per module it owns:

| Column | Content |
|---|---|
| Module | The source path, as a relative link. |
| Purpose | One sentence, present tense, describing what it does. |
| Component | The concept or guide page that explains it in prose. |
| Notes | Focused tests that cover it, or a caveat worth carrying. |

Private helpers may share a component page with the module they support, but
every module still gets its own row. Nested packages are listed as modules in
their own right — `src/playbooks/executors/llm.py` is an entry, not a footnote
under `src/playbooks/`.

Generated code is covered per **resource family**, not per file: one row naming
the family, the schema it is generated from, and the command that regenerates
it. The 1,442 files under `packages/aq-client/` are a function of
`openapi.json` and a pinned generator; they are never hand-edited.

## Coverage guarantee

Coverage is mechanical, not aspirational. Every tracked path in the repository
is assigned to exactly one shard by
[`module-ownership.json`](../../plans/documentation-overhaul/module-ownership.json),
and there is no catch-all rule — a file nobody claims is an error:

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```

```text
ok — 3702 tracked paths assigned (791 production modules)
```

Add a module without an ownership rule and that command names it and exits
non-zero.

## Related pages

* [Documentation map](../../documentation-map.md) — which ticket owns which
  page, and the rules that keep two authors out of one file.
* [Glossary](../glossary.md) — the vocabulary the catalog entries use.
* [Documentation style](../../contributing/documentation-style.md) — how to
  write an entry.
