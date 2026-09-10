# Repository map

Where things live in the Agent Queue checkout, and which documentation page
explains each area.

## Why this page exists

The repository has about 3,700 tracked files. Roughly 800 of them are tests,
another 1,400 are generated API-client code that nobody edits, and 400 are
prose. What is left — the ~790 modules that actually ship in the running system
— is spread across `src/`, `dashboard/` and `migrations/`. This page is the
index that turns "I want to change how tasks get claimed" into a directory.

For a module-by-module catalog rather than a directory-level map, use the
[module catalog](../reference/modules/README.md); every tracked path in the
repository is assigned to exactly one shard of it.

## Vocabulary

* **Shard** — the slice of the module catalog owned by one subject area, e.g.
  `scheduler` or `sessions`. See the
  [documentation map](../documentation-map.md).
* **Component page** — the concept or guide page that explains a module in
  prose. Every catalog row names one.
* **Generated artefact** — see [code generation](codegen.md).

## The top level

| Path | What it is | Tracked files | Documented by |
|---|---|---|---|
| [`src/`](../../src/) | The daemon: orchestrator, command layer, API, CLI, sessions, playbooks, database. | 592 | spread across every subject shard |
| [`dashboard/`](../../dashboard/) | The React + Vite web UI. | 407 | `docs/guides/dashboard.md` — **planned** |
| [`tests/`](../../tests/) | The pytest suite, its fixtures and its helpers. | 771 | [testing](testing.md) |
| [`packages/`](../../packages/) | The two generated API clients — never hand-edited. | 1,441 | [code generation](codegen.md) |
| [`migrations/`](../../migrations/) | Alembic revisions and environment. | 18 | [migrations](../guides/migrations.md) |
| [`scripts/`](../../scripts/) | Developer and release tooling. | 28 | [scripts](scripts.md) |
| [`.github/`](../../.github/) | Workflows and integration configuration. | 3 | [CI](ci.md) |
| [`docs/`](../README.md) | This documentation set plus the preserved historical material. | 323 | [documentation map](../documentation-map.md) |
| [`vault/`](../../vault/) | Shipped vault templates the runtime reads at run time. | 2 | `docs/concepts/configuration-and-vault.md` — **planned** |
| `.superpowers/`, `notes/`, `reports/` | Historical working material. | 106 | `docs/history/README.md` — **planned** |

Counts come from the coverage manifest and move with the tree; regenerate
rather than trusting them:

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```

```text
ok — 3746 tracked paths assigned (791 production modules)
```

## Inside `src/`

`src/` is flat at the top — around fifty modules — with subpackages for the
larger subsystems. The subject shard in the last column is where that area's
module catalog and prose live.

| Package | Owns | Shard |
|---|---|---|
| [`src/orchestrator/`](../../src/orchestrator/) | The scheduling cycle, workspace acquisition, pool measurement and reconciliation. | `scheduler`, `workspaces` |
| [`src/commands/`](../../src/commands/) | The single command handler every surface (CLI, API, MCP) dispatches through, split into mixins. | `cli` |
| [`src/cli/`](../../src/cli/) | The `aq` command line, including [`test_runner.py`](../../src/cli/test_runner.py). | `cli` |
| [`src/api/`](../../src/api/) | FastAPI routers, request/response models and the offline OpenAPI spec builder. | `api` |
| [`src/database/`](../../src/database/) | SQLAlchemy Core tables, the query modules, the engine and the migration guard. | `database` |
| [`src/sessions/`](../../src/sessions/) | Harness sessions: tmux specs, reconciler, default harnesses. | `sessions` |
| [`src/playbooks/`](../../src/playbooks/) | Playbooks V2: authoring, definition, validation, engine, runtime, executors. | `playbooks` |
| [`src/profiles/`](../../src/profiles/) | Agent profiles, the MCP server registry and profile migrations. | `routing` |
| [`src/integration/`](../../src/integration/) | Branch delivery, batches, attestation and discard. | `integration` |
| [`src/task_graph/`](../../src/task_graph/) | Formulas and the spatial graph layout engine. | `tasks` |
| [`src/resources/`](../../src/resources/) | Per-session caps, the `flock` test semaphore, process attribution. | `scheduler` |
| [`src/escalations/`](../../src/escalations/), [`src/digest/`](../../src/digest/), [`src/messages/`](../../src/messages/), [`src/discord/`](../../src/discord/) | Human-facing messaging: escalation threads, the activity digest, agent inboxes, the Discord transport. | `communications` |
| [`src/plugins/`](../../src/plugins/) | The plugin base, registry, loader and the four internal plugins. | `plugins` |
| [`src/llm/`](../../src/llm/), [`src/providers/`](../../src/providers/) | The direct LLM path and provider adapters. | `providers` |
| [`src/doctor/`](../../src/doctor/) | `aq doctor` checks. | `operations` |
| [`src/metrics/`](../../src/metrics/) | The fleet-metrics sampler. | `operations` |
| [`src/prompts/`](../../src/prompts/) | Shipped playbook markdown and reviewed playbook artefacts. | `playbooks` |

The remaining subpackages (`agents/`, `digest/`, `editor/`, `git/`,
`intelligence_classes/`, `messaging/`, `notifications/`, `panes/`, `prime/`,
`projects/`, `runtimes/`, `services/`, `skills/`, `tokens/`, `tools/`,
`integrations/`) each belong to one shard as well; the authoritative mapping is
[`module-ownership.json`](../plans/documentation-overhaul/module-ownership.json),
which is machine-checked and never guessed.

## Root files a contributor meets

| File | Purpose |
|---|---|
| [`pyproject.toml`](../../pyproject.toml) | Python package metadata, dependency pins, extras, entry points, pytest configuration and ruff configuration. One file, four jobs. |
| [`package.json`](../../package.json) / [`package-lock.json`](../../package-lock.json) | The npm workspace root: `dashboard` and `packages/aq-ts-client`. |
| [`docker-compose.yml`](../../docker-compose.yml) | The disposable PostgreSQL used by tests. |
| [`alembic.ini`](../../alembic.ini) | Alembic configuration; see [migrations](../guides/migrations.md). |
| [`openapi.json`](../../openapi.json) | The committed API schema both clients are generated from. Generated — see [codegen](codegen.md). |
| [`setup.sh`](../../setup.sh), [`uninstall.sh`](../../uninstall.sh) | Operator install and teardown. Not the contributor path — see [setup](setup.md#install). |
| [`run_tests.sh`](../../run_tests.sh), [`test_suite.bat`](../../test_suite.bat) | **Historical.** Whole-suite runners that predate `aq test`; see [scripts](scripts.md#historical-and-unsupported). |
| [`README.md`](../../README.md) | The GitHub landing page. |

## Agent-facing instruction files

Three tracked markdown files at the root are read by coding agents rather than
by people, and are part of how AQ develops itself. They are documentation, not
code, but changing them changes agent behaviour, so treat them as interface.

| File | Read by | Contains |
|---|---|---|
| [`CLAUDE.md`](../../CLAUDE.md) | The `claude` harness, automatically, at session start. | The quick reference: subsystem pointers, the testing rules, the migration prohibitions, conventions. |
| [`AGENTS.md`](../../AGENTS.md) | Harnesses that follow the `AGENTS.md` convention. | The same contract for non-Claude harnesses. |
| [`profile.md`](../../profile.md) | Loaded as the repository architecture briefing when an agent primes. | The long-form architecture, codebase map and design decisions. |

> **Note.** `CLAUDE.md` is the first thing an agent working in this repository
> reads, and it is *terse on purpose*. When it and a page under `docs/`
> disagree, the source wins; open an issue rather than quietly editing one of
> them to match the other.

`dashboard/CLAUDE.md` is the same idea scoped to the frontend.

## Directories that are not source

| Path | Why it is there |
|---|---|
| `/.aq/` | Per-checkout agent state: worktree slots, claim files, hook settings. Gitignored, generated by the session runtime, never shared. |
| `node_modules/`, `dashboard/dist/`, `packages/aq-ts-client/src/` | Build inputs and outputs. Gitignored. |
| `.pytest_cache/`, `.ruff_cache/`, `__pycache__/` | Tool caches. |
| `docs/specs/`, `docs/superpowers/`, `docs/reports/`, `docs/reviews/`, `docs/analysis/`, `docs/plans/` | Preserved historical material — the audit trail of how AQ got here. Read as background, with the date it describes, never as instructions. |

## Finding the code for a behaviour

Three moves, in order of how often they work:

1. **Ask the CLI.** `aq schema` prints the machine-readable command surface and
   `aq <group> <command> --help` the current flags. Most behaviour is reachable
   as a command, and the command names its handler.
2. **Search the catalog.** Grep
   [`module-ownership.json`](../plans/documentation-overhaul/module-ownership.json)
   for a path to get its shard, component page and category in one line.
3. **Search the tests.** The suite is one file per area,
   `tests/test_<area>.py`; the test file for a subsystem is usually a faster
   read than the subsystem. See [testing](testing.md#finding-the-right-tests).

## Related pages

* [Testing](testing.md) — the layout of `tests/` and how to run a slice of it.
* [Code generation](codegen.md) — which of these directories are written by a
  command rather than by a person.
* [Module catalog](../reference/modules/README.md) — the per-module index this
  page summarises.
* [Documentation map](../documentation-map.md) — which page owns which subject.
* [Scripts](scripts.md) — what everything in `scripts/` is for.

## Source and tests

The mapping in this page is generated and checked by
[`docs/plans/documentation-overhaul/refresh_inventory.py`](../plans/documentation-overhaul/refresh_inventory.py).

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```
