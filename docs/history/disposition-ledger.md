# Documentation disposition ledger

Every documentation file in the repository, and what was decided about it.

This ledger exists because the [documentation overhaul](../plans/documentation-overhaul/README.md)
rewrote AQ's documentation around current behaviour, and the repository already
held 508 prose files — design specs, plans, audit evidence, working notes and
guides — some accurate, some superseded, some flatly contradicting the code. The
rule the overhaul chose was **preserve, label, and point forward**: nothing
useful is deleted to resolve a contradiction, but nothing stale is left looking
like current instructions either.

If you are looking for the *current* documentation, start at
[the documentation home](../README.md). This page is for answering "what
happened to the page I remember" and "is this file still true".

## The five dispositions

| Disposition | Meaning | What was done to the file |
|---|---|---|
| `current` | Accurate against the code on `main`. | Nothing, or a cross-link added. |
| `update` | Broadly right, with a specific claim that was wrong or a scope that was not stated. | The claim was corrected, or a banner states which mode or version it applies to. |
| `redirect` | Superseded by a new page. | Reduced to a short stub that names the replacement, so the old path still resolves. |
| `archive` | Describes a procedure or component that no longer exists, and has no direct replacement page. | Kept at its path with a **Retired** banner naming what to read instead. Delisted from navigation. |
| `historical` | Design history or audit evidence, preserved on purpose as the record of how AQ got here. | Kept, with a banner giving its genre and saying it is not current documentation. Bodies of evidence bundles were not edited at all. |

Counts: `current` 121, `update` 3, `redirect` 7,
`archive` 8, `historical` 369.

## What "immutable evidence" means here

Four trees are audit evidence rather than prose: [`docs/reports/`](../reports/README.md),
[`docs/reviews/`](../reviews/README.md), [`docs/gates/`](../gates/README.md),
[`docs/superpowers/reports/`](../superpowers/reports/README.md),
[`reports/`](../../reports/README.md) and
[`.superpowers/sdd/`](../../.superpowers/README.md). Their value is that they say
what was observed on a date, so editing them would destroy the thing they are
kept for. This ticket added a directory index beside each and changed nothing
inside — with one exception, recorded in the row for
`docs/reports/integration-safeguards-2026-09-09/SOURCE-INDEX.md`: all 549 of its
source links pointed one directory level too high and resolved to nothing, so the
prefix was corrected. No finding, count or conclusion changed.

## Scope

Rows are every tracked path that the coverage manifest assigns to the `legacy`
shard, plus every other path it categorises as `documentation` — those are marked
`current` because the ticket named in their note wrote them for this overhaul.

Prose that lives beside the code it documents (`src/`, `tests/`, `packages/`,
`dashboard/`, `vault/`) is **not** here. That includes shipped agent-facing
content such as `src/skills/*/SKILL.md`, `src/prompts/`, `CLAUDE.md`, `AGENTS.md`
and `profile.md`: it is loaded at run time, so changing it changes agent
behaviour and belongs to the ticket that owns the code, not to a documentation
reconciliation. Two stale claims found in that content during this work are
recorded in
[known-inaccuracies.md](../plans/documentation-overhaul/known-inaccuracies.md).

## Verifying this page

```bash
python3 docs/history/check_dispositions.py
```

The check fails if a file in scope has no row, a row names a path that no longer
exists, a disposition is not one of the five, or a page whose disposition
requires a banner does not carry one.

## Repository root

Documentation that sits beside the code rather than under `docs/`.

| Path | Disposition | Note |
|---|---|---|
| [`goals.md`](../../goals.md) | `historical` | Original product goals, 2026-03. |
| [`mkdocs.yml`](../../mkdocs.yml) | `archive` | Configures a MkDocs Material site at `electricjack.github.io/agent-queue/`. **Recorded decision: GitHub-rendered Markdown is the publication format and this site is unmaintained** — 49 of the 77 pages in its `nav:` do not exist. The decision is written into the file's own header rather than deleting it, so nobody reinstates a Pages deploy with no config. `.github/workflows/docs.yml` still builds and deploys it on pushes to `main` touching `docs/**`; removing that is an infrastructure change in files this ticket does not own, [recorded as a finding](../plans/documentation-overhaul/known-inaccuracies.md). |
| [`notes.md`](../../notes.md) | `historical` | Scratch notes kept with the repository. |
| [`requirements.md`](../../requirements.md) | `historical` | Original requirements list. |
| [`test-coverage-impl-platform.md`](../../test-coverage-impl-platform.md) | `historical` | Point-in-time coverage report for the platform work. |

## Repository root — owned by other shards

Agent-facing and landing-page prose. Listed for completeness; the ticket in each note owns the file.

| Path | Disposition | Note |
|---|---|---|
| [`AGENTS.md`](../../AGENTS.md) | `current` | Owned by the `contributing` ticket; explained in `docs/contributing/repo-map.md`. |
| [`CLAUDE.md`](../../CLAUDE.md) | `current` | Owned by the `contributing` ticket; explained in `docs/contributing/repo-map.md`. |
| [`README.md`](../../README.md) | `current` | Owned by the `readme` ticket. |
| [`profile.md`](../../profile.md) | `current` | Owned by the `contributing` ticket; explained in `docs/contributing/repo-map.md`. |

## `docs/` top level

Pages that were never filed into a subdirectory.

| Path | Disposition | Note |
|---|---|---|
| [`docs/README.md`](../README.md) | `current` | Owned by the `foundation` ticket. |
| [`docs/agent-questions.md`](../agent-questions.md) | `current` | Accurate against `src/questions/`; now links [Messaging](../concepts/messaging.md) for the surrounding model. |
| [`docs/agent-queue-primitives.md`](../agent-queue-primitives.md) | `historical` | Primitive map, last revised 2026-09-02. [System architecture](../concepts/architecture.md) is the maintained version. |
| [`docs/breakdown.md`](../breakdown.md) | `historical` | The same Mermaid map without prose; superseded by [System architecture](../concepts/architecture.md). |
| [`docs/documentation-map.md`](../documentation-map.md) | `current` | Owned by `foundation`. |
| [`docs/index.md`](../index.md) | `redirect` | Second landing page; claimed SQLite-backed state. Reduced to a pointer to the [documentation home](../README.md); the screenshots stay here. |

## `docs/history/` — this shard's own pages

| Path | Disposition | Note |
|---|---|---|
| [`docs/history/README.md`](README.md) | `current` | Written by this ticket. The historical index: what is preserved, why, and how to read it. |
| [`docs/history/check_dispositions.py`](check_dispositions.py) | `current` | Written by this ticket. The check that keeps this ledger complete. |
| [`docs/history/disposition-ledger.md`](disposition-ledger.md) | `current` | Written by this ticket. This ledger. |

## `docs/concepts/`

| Path | Disposition | Note |
|---|---|---|
| [`docs/concepts/README.md`](../concepts/README.md) | `current` | Written by this ticket. Directory index and beginner reading order for the concept pages. |
| [`docs/concepts/agents-and-routing.md`](../concepts/agents-and-routing.md) | `current` | Owned by the `routing` ticket. |
| [`docs/concepts/architecture.md`](../concepts/architecture.md) | `current` | Owned by the `architecture` ticket. |
| [`docs/concepts/integration.md`](../concepts/integration.md) | `current` | Owned by the `integration` ticket. |
| [`docs/concepts/messaging.md`](../concepts/messaging.md) | `current` | Owned by the `communications` ticket. |
| [`docs/concepts/playbooks.md`](../concepts/playbooks.md) | `current` | Owned by the `playbooks` ticket. |
| [`docs/concepts/providers.md`](../concepts/providers.md) | `current` | Owned by the `providers` ticket. |
| [`docs/concepts/scheduling.md`](../concepts/scheduling.md) | `current` | Owned by the `scheduler` ticket. |
| [`docs/concepts/sessions.md`](../concepts/sessions.md) | `current` | Owned by the `sessions` ticket. |
| [`docs/concepts/tasks.md`](../concepts/tasks.md) | `current` | Owned by the `tasks` ticket. |

## `docs/guides/`

Split between current guides and retired ones. Every retired page keeps its old path and carries a banner naming its replacement.

| Path | Disposition | Note |
|---|---|---|
| [`docs/guides/README.md`](../guides/README.md) | `current` | Written by this ticket. Directory index; separates current guides from retired ones. |
| [`docs/guides/agent-tools.md`](../guides/agent-tools.md) | `redirect` | Superseded by [Agent-facing tools](../reference/cli/agent-tools.md). |
| [`docs/guides/architecture.md`](../guides/architecture.md) | `redirect` | Superseded by [System architecture](../concepts/architecture.md). |
| [`docs/guides/cli.md`](../guides/cli.md) | `redirect` | Superseded by [the CLI reference](../reference/cli/README.md). |
| [`docs/guides/development-integration.md`](../guides/development-integration.md) | `current` | Owned by the `integration` ticket. |
| [`docs/guides/discord-commands.md`](../guides/discord-commands.md) | `redirect` | Accurate but thin; superseded by [Messaging](../concepts/messaging.md) and [Escalations](../guides/escalations.md). |
| [`docs/guides/discord-migration.md`](../guides/discord-migration.md) | `archive` | Operator runbook for the single-channel cutover, which completed on 2026-09-08. |
| [`docs/guides/discord-replacement-checklist.md`](../guides/discord-replacement-checklist.md) | `archive` | The cutover's readiness ledger; retained as the record of what replaced what. |
| [`docs/guides/e2e-swarm.md`](../guides/e2e-swarm.md) | `current` | Referenced by `CLAUDE.md` and [local checks](../contributing/checks.md). |
| [`docs/guides/escalations.md`](../guides/escalations.md) | `current` | Owned by the `communications` ticket. |
| [`docs/guides/feature-merge-history.md`](../guides/feature-merge-history.md) | `update` | Describes ancestry in the optional strict integration modes; banner added to say it is not the development-mode path. |
| [`docs/guides/getting-started.md`](../guides/getting-started.md) | `redirect` | Listed a Discord bot token as a prerequisite. Superseded by [Install](../tutorials/install.md) and [Your first task](../tutorials/first-task.md). |
| [`docs/guides/hierarchical-integration-trains.md`](../guides/hierarchical-integration-trains.md) | `update` | Optional per-project strict mode, off by default. Its SQLite upgrade claim is corrected. |
| [`docs/guides/integration-ci-boundaries.md`](../guides/integration-ci-boundaries.md) | `current` | Matches `.github/workflows/tests.yml` as of 2026-09-07. |
| [`docs/guides/integration-migration.md`](../guides/integration-migration.md) | `current` | Owned in practice by the integration work; cross-links the concept page. |
| [`docs/guides/integration-troubleshooting.md`](../guides/integration-troubleshooting.md) | `current` |  |
| [`docs/guides/llm-providers.md`](../guides/llm-providers.md) | `current` |  |
| [`docs/guides/merge-gating.md`](../guides/merge-gating.md) | `historical` | Incident write-up for the 2026-09-03 red-CI merge; the policy it argued for is now in [CI at integration boundaries](../guides/integration-ci-boundaries.md). |
| [`docs/guides/migrations.md`](../guides/migrations.md) | `current` | Now a policy page; the inventory framing was removed. |
| [`docs/guides/playbook-v2-cutover-report-template.md`](../guides/playbook-v2-cutover-report-template.md) | `archive` | Report template for that same cutover. |
| [`docs/guides/playbook-v2-cutover-runbook.md`](../guides/playbook-v2-cutover-runbook.md) | `archive` | Drives a V1 runtime that was deleted in the 2026-09-04 cutover. |
| [`docs/guides/project-onboarding.md`](../guides/project-onboarding.md) | `current` | Owned by the `workspaces` ticket. |
| [`docs/guides/resource-gating.md`](../guides/resource-gating.md) | `current` | Owned by the `scheduler` ticket. |
| [`docs/guides/runtime-development-guide.md`](../guides/runtime-development-guide.md) | `archive` | Near-duplicate of the page above, and linked a page that never existed. |
| [`docs/guides/runtime-development.md`](../guides/runtime-development.md) | `archive` | Describes adding an in-process runtime backend. There are no in-tree runtimes; a profile's `harness` is the only selector. |
| [`docs/guides/session-troubleshooting.md`](../guides/session-troubleshooting.md) | `current` |  |
| [`docs/guides/task-state-machine.md`](../guides/task-state-machine.md) | `redirect` | Superseded by [Tasks, epics, dependencies and task graphs](../concepts/tasks.md). |
| [`docs/guides/upgrade-integration-mode.md`](../guides/upgrade-integration-mode.md) | `archive` | One-off upgrade note for Alembic revision `c4d5e6f7a8b9`, folded into the squashed baseline. |
| [`docs/guides/worker-pools.md`](../guides/worker-pools.md) | `current` | Owned by the `scheduler` ticket. |

## `docs/tutorials/`

| Path | Disposition | Note |
|---|---|---|
| [`docs/tutorials/README.md`](../tutorials/README.md) | `current` | Owned by the `quickstart` ticket. |
| [`docs/tutorials/first-task.md`](../tutorials/first-task.md) | `current` | Owned by the `quickstart` ticket. |
| [`docs/tutorials/install.md`](../tutorials/install.md) | `current` | Owned by the `quickstart` ticket. |

## `docs/reference/`

| Path | Disposition | Note |
|---|---|---|
| [`docs/reference/api/README.md`](../reference/api/README.md) | `current` | Owned by the `api` ticket. |
| [`docs/reference/api/conventions.md`](../reference/api/conventions.md) | `current` | Owned by the `api` ticket. |
| [`docs/reference/api/events.md`](../reference/api/events.md) | `current` | Owned by the `api` ticket. |
| [`docs/reference/api/models.md`](../reference/api/models.md) | `current` | Owned by the `api` ticket. |
| [`docs/reference/api/python-client.md`](../reference/api/python-client.md) | `current` | Owned by the `api` ticket. |
| [`docs/reference/api/typescript-client.md`](../reference/api/typescript-client.md) | `current` | Owned by the `api` ticket. |
| [`docs/reference/cli-command-inventory.json`](../reference/cli-command-inventory.json) | `current` | Generated by `scripts/generate-cli-command-inventory.py`; never hand-edited. |
| [`docs/reference/cli-command-inventory.md`](../reference/cli-command-inventory.md) | `update` | Accurate; a forward link to [the CLI reference](../reference/cli/README.md) was missing and is added. |
| [`docs/reference/cli/README.md`](../reference/cli/README.md) | `current` | Owned by the `cli` ticket. |
| [`docs/reference/cli/agent-tools.md`](../reference/cli/agent-tools.md) | `current` | Owned by the `cli` ticket. |
| [`docs/reference/cli/commands.md`](../reference/cli/commands.md) | `current` | Owned by the `cli` ticket. |
| [`docs/reference/cli/contracts.md`](../reference/cli/contracts.md) | `current` | Owned by the `cli` ticket. |
| [`docs/reference/cli/prime.md`](../reference/cli/prime.md) | `current` | Owned by the `cli` ticket. |
| [`docs/reference/database/README.md`](../reference/database/README.md) | `current` | Owned by the `database` ticket. |
| [`docs/reference/database/data-lifecycle.md`](../reference/database/data-lifecycle.md) | `current` | Owned by the `database` ticket. |
| [`docs/reference/database/migrations.md`](../reference/database/migrations.md) | `current` | Owned by the `database` ticket. |
| [`docs/reference/database/queries.md`](../reference/database/queries.md) | `current` | Owned by the `database` ticket. |
| [`docs/reference/database/tables.md`](../reference/database/tables.md) | `current` | Owned by the `database` ticket. |
| [`docs/reference/glossary.md`](../reference/glossary.md) | `current` | Owned by the `foundation` ticket. |
| [`docs/reference/harnesses.md`](../reference/harnesses.md) | `current` | Written with [Sessions](../concepts/sessions.md); assigned to this shard only because it predates the ownership table. |
| [`docs/reference/modules/README.md`](../reference/modules/README.md) | `current` | Owned by the `foundation` ticket. |
| [`docs/reference/modules/api.md`](../reference/modules/api.md) | `current` | Owned by the `api` ticket. |
| [`docs/reference/modules/architecture.md`](../reference/modules/architecture.md) | `current` | Owned by the `architecture` ticket. |
| [`docs/reference/modules/cli.md`](../reference/modules/cli.md) | `current` | Owned by the `cli` ticket. |
| [`docs/reference/modules/communications.md`](../reference/modules/communications.md) | `current` | Owned by the `communications` ticket. |
| [`docs/reference/modules/contributing.md`](../reference/modules/contributing.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/reference/modules/database.md`](../reference/modules/database.md) | `current` | Owned by the `database` ticket. |
| [`docs/reference/modules/integration.md`](../reference/modules/integration.md) | `current` | Owned by the `integration` ticket. |
| [`docs/reference/modules/playbooks.md`](../reference/modules/playbooks.md) | `current` | Owned by the `playbooks` ticket. |
| [`docs/reference/modules/providers.md`](../reference/modules/providers.md) | `current` | Owned by the `providers` ticket. |
| [`docs/reference/modules/routing.md`](../reference/modules/routing.md) | `current` | Owned by the `routing` ticket. |
| [`docs/reference/modules/scheduler.md`](../reference/modules/scheduler.md) | `current` | Owned by the `scheduler` ticket. |
| [`docs/reference/modules/sessions.md`](../reference/modules/sessions.md) | `current` | Owned by the `sessions` ticket. |
| [`docs/reference/modules/tasks.md`](../reference/modules/tasks.md) | `current` | Owned by the `tasks` ticket. |
| [`docs/reference/profiles-and-classes.md`](../reference/profiles-and-classes.md) | `current` | Written with [Agents and routing](../concepts/agents-and-routing.md). |
| [`docs/reference/terminals-and-claims.md`](../reference/terminals-and-claims.md) | `current` | As above. |
| [`docs/reference/usage-accounting.md`](../reference/usage-accounting.md) | `current` | Written with [Providers](../concepts/providers.md). |

## `docs/contributing/`

| Path | Disposition | Note |
|---|---|---|
| [`docs/contributing/README.md`](../contributing/README.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/checks.md`](../contributing/checks.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/ci.md`](../contributing/ci.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/codegen.md`](../contributing/codegen.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/documentation-style.md`](../contributing/documentation-style.md) | `current` | Owned by the `foundation` ticket. |
| [`docs/contributing/pull-requests.md`](../contributing/pull-requests.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/releases.md`](../contributing/releases.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/repo-map.md`](../contributing/repo-map.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/scripts.md`](../contributing/scripts.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/setup.md`](../contributing/setup.md) | `current` | Owned by the `contributing` ticket. |
| [`docs/contributing/testing.md`](../contributing/testing.md) | `current` | Owned by the `contributing` ticket. |

## `docs/specs/` — design specs

The cross-cutting design record. Historical: a spec states intent at approval and is not revised against the code.

| Path | Disposition | Note |
|---|---|---|
| [`docs/specs/.obsidian/app.json`](../specs/.obsidian/app.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/appearance.json`](../specs/.obsidian/appearance.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/community-plugins.json`](../specs/.obsidian/community-plugins.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/core-plugins.json`](../specs/.obsidian/core-plugins.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/graph.json`](../specs/.obsidian/graph.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/plugins/claude-code-integration/main.js`](../specs/.obsidian/plugins/claude-code-integration/main.js) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/plugins/claude-code-integration/manifest.json`](../specs/.obsidian/plugins/claude-code-integration/manifest.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/plugins/claude-code-integration/styles.css`](../specs/.obsidian/plugins/claude-code-integration/styles.css) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/plugins/obsidian42-brat/brat-migrations.json`](../specs/.obsidian/plugins/obsidian42-brat/brat-migrations.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/plugins/obsidian42-brat/data.json`](../specs/.obsidian/plugins/obsidian42-brat/data.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/plugins/obsidian42-brat/main.js`](../specs/.obsidian/plugins/obsidian42-brat/main.js) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/plugins/obsidian42-brat/manifest.json`](../specs/.obsidian/plugins/obsidian42-brat/manifest.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/plugins/obsidian42-brat/styles.css`](../specs/.obsidian/plugins/obsidian42-brat/styles.css) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/.obsidian/workspace.json`](../specs/.obsidian/workspace.json) | `historical` | Obsidian vault settings for browsing `docs/specs/`; not read by AQ. |
| [`docs/specs/README.md`](../specs/README.md) | `current` | Written by this ticket. Directory index and historical banner for the design specs. |
| [`docs/specs/agent-profiles.md`](../specs/agent-profiles.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/command-handler.md`](../specs/command-handler.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/config.md`](../specs/config.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/database.md`](../specs/database.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/README.md`](../specs/design/README.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/agent-coordination.md`](../specs/design/agent-coordination.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/aq-surface.md`](../specs/design/aq-surface.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/development-integration.md`](../specs/design/development-integration.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/feature-pauses.md`](../specs/design/feature-pauses.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/fleet-metrics.md`](../specs/design/fleet-metrics.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/formulas.md`](../specs/design/formulas.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/guiding-design-principles.md`](../specs/design/guiding-design-principles.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/memory-plugin.md`](../specs/design/memory-plugin.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/memory-scoping.md`](../specs/design/memory-scoping.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/messaging-rework.md`](../specs/design/messaging-rework.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/playbooks.md`](../specs/design/playbooks.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/profiles.md`](../specs/design/profiles.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/roadmap.md`](../specs/design/roadmap.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/sandboxed-playbooks.md`](../specs/design/sandboxed-playbooks.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/self-improvement.md`](../specs/design/self-improvement.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/session-runtime.md`](../specs/design/session-runtime.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/supervisor-agent.md`](../specs/design/supervisor-agent.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/trust-and-ops.md`](../specs/design/trust-and-ops.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/vault.md`](../specs/design/vault.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/work-graph.md`](../specs/design/work-graph.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/workspaces-v2.md`](../specs/design/workspaces-v2.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/design/worktree-execution.md`](../specs/design/worktree-execution.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/event-bus.md`](../specs/event-bus.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/git.md`](../specs/git.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/README.md`](../specs/implementation/README.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/aq-surface.md`](../specs/implementation/aq-surface.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/feature-pauses.md`](../specs/implementation/feature-pauses.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/messaging-rework.md`](../specs/implementation/messaging-rework.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/session-runtime.md`](../specs/implementation/session-runtime.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/supervisor-agent.md`](../specs/implementation/supervisor-agent.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/trust-and-ops.md`](../specs/implementation/trust-and-ops.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/work-graph.md`](../specs/implementation/work-graph.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/implementation/worktree-execution.md`](../specs/implementation/worktree-execution.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/llm-logging.md`](../specs/llm-logging.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/main.md`](../specs/main.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/mcp-server.md`](../specs/mcp-server.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/memory-consolidation.md`](../specs/memory-consolidation.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/messaging/README.md`](../specs/messaging/README.md) | `current` | Written by this ticket. Directory index; both specs are superseded. |
| [`docs/specs/messaging/base.md`](../specs/messaging/base.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/messaging/discord.md`](../specs/messaging/discord.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/models-and-state-machine.md`](../specs/models-and-state-machine.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/orchestrator.md`](../specs/orchestrator.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/packaging.md`](../specs/packaging.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/plan-parser.md`](../specs/plan-parser.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/plugin-system.md`](../specs/plugin-system.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/prompt-builder.md`](../specs/prompt-builder.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/reflection.md`](../specs/reflection.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/runtimes/README.md`](../specs/runtimes/README.md) | `current` | Written by this ticket. Directory index; no runtimes ship. |
| [`docs/specs/runtimes/acpx.md`](../specs/runtimes/acpx.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/runtimes/claude_sdk.md`](../specs/runtimes/claude_sdk.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/runtimes/development-guide.md`](../specs/runtimes/development-guide.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/schedule.md`](../specs/schedule.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/scheduler-and-budget.md`](../specs/scheduler-and-budget.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/setup-wizard.md`](../specs/setup-wizard.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/supervisor.md`](../specs/supervisor.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |
| [`docs/specs/tiered-tools.md`](../specs/tiered-tools.md) | `historical` | Design record: intended behaviour when approved, not maintained against the code. |

## `docs/superpowers/` — per-feature design history

One dated design, plan or evidence bundle per feature.

| Path | Disposition | Note |
|---|---|---|
| [`docs/superpowers/README.md`](../superpowers/README.md) | `current` | Written by this ticket. Directory index for the per-feature design history. |
| [`docs/superpowers/plans/2026-04-25-aq-memory-extraction.md`](../superpowers/plans/2026-04-25-aq-memory-extraction.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-04-25-platforms-implementation.md`](../superpowers/plans/2026-04-25-platforms-implementation.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-04-27-moss-spade-email-sandboxing.md`](../superpowers/plans/2026-04-27-moss-spade-email-sandboxing.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-05-07-agent-reconciliation-implementation.md`](../superpowers/plans/2026-05-07-agent-reconciliation-implementation.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-05-07-workspaces-v2-implementation.md`](../superpowers/plans/2026-05-07-workspaces-v2-implementation.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-20-supervisor-p3-p5.md`](../superpowers/plans/2026-08-20-supervisor-p3-p5.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-20-wave3-three-lanes.md`](../superpowers/plans/2026-08-20-wave3-three-lanes.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-dv2-phase1-control-plane.md`](../superpowers/plans/2026-08-21-dv2-phase1-control-plane.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-dv2-phase2-review-policy.md`](../superpowers/plans/2026-08-21-dv2-phase2-review-policy.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-dv2-phase3-dashboard-shell.md`](../superpowers/plans/2026-08-21-dv2-phase3-dashboard-shell.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-dv2-phase4-command-center.md`](../superpowers/plans/2026-08-21-dv2-phase4-command-center.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-dv2-phase5-observability.md`](../superpowers/plans/2026-08-21-dv2-phase5-observability.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-dv2-phase6-spec-ingestion.md`](../superpowers/plans/2026-08-21-dv2-phase6-spec-ingestion.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-legacy-chat-removal.md`](../superpowers/plans/2026-08-21-legacy-chat-removal.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-wave4-auth-s2.md`](../superpowers/plans/2026-08-21-wave4-auth-s2.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-wave4-dashboard-d1-d4.md`](../superpowers/plans/2026-08-21-wave4-dashboard-d1-d4.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-wave4-discord-e2e.md`](../superpowers/plans/2026-08-21-wave4-discord-e2e.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-21-wave4-discord-live-test.md`](../superpowers/plans/2026-08-21-wave4-discord-live-test.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-dashboard-shell-v2-plan.md`](../superpowers/plans/2026-08-22-dashboard-shell-v2-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-console-stream-plan.md`](../superpowers/plans/2026-08-22-pane-console-stream-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-contextual-settings-plan.md`](../superpowers/plans/2026-08-22-pane-contextual-settings-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-diff-review-changes-plan.md`](../superpowers/plans/2026-08-22-pane-diff-review-changes-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-file-browser-plan.md`](../superpowers/plans/2026-08-22-pane-file-browser-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-playbook-run-inspector-plan.md`](../superpowers/plans/2026-08-22-pane-playbook-run-inspector-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-plugin-interface-plan.md`](../superpowers/plans/2026-08-22-pane-plugin-interface-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-proposal-preview-plan.md`](../superpowers/plans/2026-08-22-pane-proposal-preview-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-session-peek-plan.md`](../superpowers/plans/2026-08-22-pane-session-peek-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-spec-doc-reader-plan.md`](../superpowers/plans/2026-08-22-pane-spec-doc-reader-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-22-pane-task-detail-plan.md`](../superpowers/plans/2026-08-22-pane-task-detail-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-24-worktree-workspace-migration.md`](../superpowers/plans/2026-08-24-worktree-workspace-migration.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-25-live-pane-streaming.md`](../superpowers/plans/2026-08-25-live-pane-streaming.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-28-swarm-hierarchy.md`](../superpowers/plans/2026-08-28-swarm-hierarchy.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-29-swarm-claims-pools.md`](../superpowers/plans/2026-08-29-swarm-claims-pools.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-29-swarm-formulas.md`](../superpowers/plans/2026-08-29-swarm-formulas.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-30-agent-flock.md`](../superpowers/plans/2026-08-30-agent-flock.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-30-agent-question-routing.md`](../superpowers/plans/2026-08-30-agent-question-routing.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-30-command-center-unification.md`](../superpowers/plans/2026-08-30-command-center-unification.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-30-llm-direct-path.md`](../superpowers/plans/2026-08-30-llm-direct-path.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-31-comment-project-identity.md`](../superpowers/plans/2026-08-31-comment-project-identity.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-31-integration-mode-unification.md`](../superpowers/plans/2026-08-31-integration-mode-unification.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-31-mandatory-triage-playbook.md`](../superpowers/plans/2026-08-31-mandatory-triage-playbook.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-31-playbook-intelligence-routing.md`](../superpowers/plans/2026-08-31-playbook-intelligence-routing.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-08-31-task-session-history.md`](../superpowers/plans/2026-08-31-task-session-history.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-contracts-intent.md`](../superpowers/plans/2026-09-01-playbook-v2-contracts-intent.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md`](../superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-durable-state-storage.md`](../superpowers/plans/2026-09-01-playbook-v2-durable-state-storage.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-engine-executors.md`](../superpowers/plans/2026-09-01-playbook-v2-engine-executors.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md`](../superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md`](../superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md`](../superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-phase0-security.md`](../superpowers/plans/2026-09-01-playbook-v2-phase0-security.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md`](../superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-pools-exit-gate-a.md`](../superpowers/plans/2026-09-01-pools-exit-gate-a.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-task-graph-layout-api-plan.md`](../superpowers/plans/2026-09-01-task-graph-layout-api-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-task-graph-layout-dashboard-plan.md`](../superpowers/plans/2026-09-01-task-graph-layout-dashboard-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-01-task-graph-layout-engine-plan.md`](../superpowers/plans/2026-09-01-task-graph-layout-engine-plan.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-02-deliverable-close-check.md`](../superpowers/plans/2026-09-02-deliverable-close-check.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-04-hierarchical-integration-trains-implementation.md`](../superpowers/plans/2026-09-04-hierarchical-integration-trains-implementation.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-04-playbook-v2-fresh-cutover.md`](../superpowers/plans/2026-09-04-playbook-v2-fresh-cutover.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/2026-09-05-project-onboarding-service.md`](../superpowers/plans/2026-09-05-project-onboarding-service.md) | `historical` | Implementation plan for one feature, written before the work. |
| [`docs/superpowers/plans/README.md`](../superpowers/plans/README.md) | `current` | Written by this ticket. Dated index of the 60 implementation plans. |
| [`docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/01-branching.png`](../superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/01-branching.png) | `historical` | Scenario evidence captured while the feature was built; preserved unedited. |
| [`docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/02-convergence.png`](../superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/02-convergence.png) | `historical` | Scenario evidence captured while the feature was built; preserved unedited. |
| [`docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/03-loop.png`](../superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/03-loop.png) | `historical` | Scenario evidence captured while the feature was built; preserved unedited. |
| [`docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/04-ai-node.png`](../superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/04-ai-node.png) | `historical` | Scenario evidence captured while the feature was built; preserved unedited. |
| [`docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/05-stale-contract.png`](../superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/05-stale-contract.png) | `historical` | Scenario evidence captured while the feature was built; preserved unedited. |
| [`docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/06-diff-review.png`](../superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/06-diff-review.png) | `historical` | Scenario evidence captured while the feature was built; preserved unedited. |
| [`docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/07-run-overlay-old-artifact.png`](../superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/07-run-overlay-old-artifact.png) | `historical` | Scenario evidence captured while the feature was built; preserved unedited. |
| [`docs/superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/README.md`](../superpowers/reports/2026-09-01-playbook-v2-pkg5-scenarios/README.md) | `historical` | Scenario evidence captured while the feature was built; preserved unedited. |
| [`docs/superpowers/reports/README.md`](../superpowers/reports/README.md) | `current` | Written by this ticket. Index of the unedited feature-evidence bundles. |
| [`docs/superpowers/specs/2026-04-25-aq-memory-extraction-design.md`](../superpowers/specs/2026-04-25-aq-memory-extraction-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-04-25-platforms-implementation-design.md`](../superpowers/specs/2026-04-25-platforms-implementation-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-04-25-profile-validation-design.md`](../superpowers/specs/2026-04-25-profile-validation-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-04-27-moss-spade-email-sandboxing-design.md`](../superpowers/specs/2026-04-27-moss-spade-email-sandboxing-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-04-27-runtime-rename-and-acp-design.md`](../superpowers/specs/2026-04-27-runtime-rename-and-acp-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md`](../superpowers/specs/2026-05-07-agent-reconciliation-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-21-dashboard-v2-and-work-pipeline-design.md`](../superpowers/specs/2026-08-21-dashboard-v2-and-work-pipeline-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-dashboard-shell-v2-design.md`](../superpowers/specs/2026-08-22-dashboard-shell-v2-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-console-stream-design.md`](../superpowers/specs/2026-08-22-pane-console-stream-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-contextual-settings-design.md`](../superpowers/specs/2026-08-22-pane-contextual-settings-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-diff-review-changes-design.md`](../superpowers/specs/2026-08-22-pane-diff-review-changes-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-file-browser-design.md`](../superpowers/specs/2026-08-22-pane-file-browser-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-playbook-run-inspector-design.md`](../superpowers/specs/2026-08-22-pane-playbook-run-inspector-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-plugin-interface-design.md`](../superpowers/specs/2026-08-22-pane-plugin-interface-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-proposal-preview-design.md`](../superpowers/specs/2026-08-22-pane-proposal-preview-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-session-peek-design.md`](../superpowers/specs/2026-08-22-pane-session-peek-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-spec-doc-reader-design.md`](../superpowers/specs/2026-08-22-pane-spec-doc-reader-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-22-pane-task-detail-design.md`](../superpowers/specs/2026-08-22-pane-task-detail-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-24-usage-aware-concurrency.md`](../superpowers/specs/2026-08-24-usage-aware-concurrency.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-25-live-pane-streaming-design.md`](../superpowers/specs/2026-08-25-live-pane-streaming-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-27-session-desired-state-design.md`](../superpowers/specs/2026-08-27-session-desired-state-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-28-swarm-work-model-design.md`](../superpowers/specs/2026-08-28-swarm-work-model-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-30-agent-flock-design.md`](../superpowers/specs/2026-08-30-agent-flock-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-30-agent-question-routing.md`](../superpowers/specs/2026-08-30-agent-question-routing.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-30-command-center-playbooks-proposal.md`](../superpowers/specs/2026-08-30-command-center-playbooks-proposal.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-30-command-center-unification-design.md`](../superpowers/specs/2026-08-30-command-center-unification-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-30-llm-direct-path-design.md`](../superpowers/specs/2026-08-30-llm-direct-path-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-30-projectless-supervisor-design.md`](../superpowers/specs/2026-08-30-projectless-supervisor-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-31-mandatory-triage-playbook-design.md`](../superpowers/specs/2026-08-31-mandatory-triage-playbook-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-08-31-playbook-intelligence-routing-design.md`](../superpowers/specs/2026-08-31-playbook-intelligence-routing-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-01-dashboard-vitest-flakiness.md`](../superpowers/specs/2026-09-01-dashboard-vitest-flakiness.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md`](../superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md`](../superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-03-project-onboarding-design.md`](../superpowers/specs/2026-09-03-project-onboarding-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-04-dashboard-performance-implementation.md`](../superpowers/specs/2026-09-04-dashboard-performance-implementation.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-04-dashboard-performance-investigation.md`](../superpowers/specs/2026-09-04-dashboard-performance-investigation.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md`](../superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-review.md`](../superpowers/specs/2026-09-04-hierarchical-integration-trains-review.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-04-playbook-v2-fresh-cutover-design.md`](../superpowers/specs/2026-09-04-playbook-v2-fresh-cutover-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-05-ci-main-sentinel-design.md`](../superpowers/specs/2026-09-05-ci-main-sentinel-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md`](../superpowers/specs/2026-09-06-assignment-routing-as-playbook.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-06-blocked-task-escalation-design.md`](../superpowers/specs/2026-09-06-blocked-task-escalation-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-06-release-roadmap.md`](../superpowers/specs/2026-09-06-release-roadmap.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-07-project-folders-and-drag-drop-design.md`](../superpowers/specs/2026-09-07-project-folders-and-drag-drop-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-07-provider-usage-design.md`](../superpowers/specs/2026-09-07-provider-usage-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-07-provider-usage-implementation.md`](../superpowers/specs/2026-09-07-provider-usage-implementation.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-07-recent-work-and-model-attribution-design.md`](../superpowers/specs/2026-09-07-recent-work-and-model-attribution-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-07-sqlite-removal-implementation.md`](../superpowers/specs/2026-09-07-sqlite-removal-implementation.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-08-discord-simplification-implementation.md`](../superpowers/specs/2026-09-08-discord-simplification-implementation.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-08-global-worker-pools-design.md`](../superpowers/specs/2026-09-08-global-worker-pools-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/2026-09-08-task-deletion-with-materialized-branches-design.md`](../superpowers/specs/2026-09-08-task-deletion-with-materialized-branches-design.md) | `historical` | Feature design or implementation spec, written before or during the work. |
| [`docs/superpowers/specs/README.md`](../superpowers/specs/README.md) | `current` | Written by this ticket. Dated index of the 51 feature designs. |

## `docs/analysis/`

Point-in-time comparisons and assessments.

| Path | Disposition | Note |
|---|---|---|
| [`docs/analysis/2026-08-26-session-runtime-vs-gascity.md`](../analysis/2026-08-26-session-runtime-vs-gascity.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/2026-08-28-beads-properties-and-parity.md`](../analysis/2026-08-28-beads-properties-and-parity.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/2026-08-28-beads-swarm-migration-evaluation.md`](../analysis/2026-08-28-beads-swarm-migration-evaluation.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/2026-09-01-resource-gating-verification.md`](../analysis/2026-09-01-resource-gating-verification.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/2026-09-02-pools-final-acceptance.md`](../analysis/2026-09-02-pools-final-acceptance.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/README.md`](../analysis/README.md) | `current` | Written by this ticket. Dated index of the analysis pages. |
| [`docs/analysis/comparison-gascity-beads.md`](../analysis/comparison-gascity-beads.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/ecosystem-positioning.md`](../analysis/ecosystem-positioning.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/execution-plan.md`](../analysis/execution-plan.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/extensibility-architecture.md`](../analysis/extensibility-architecture.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/framework-overhaul-todo.md`](../analysis/framework-overhaul-todo.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |
| [`docs/analysis/performance-assessment.md`](../analysis/performance-assessment.md) | `historical` | Comparative or point-in-time analysis; conclusions were acted on elsewhere. |

## `docs/reports/` — audit evidence

Immutable: indexed, never edited. The one exception is recorded in the note.

| Path | Disposition | Note |
|---|---|---|
| [`docs/reports/README.md`](../reports/README.md) | `current` | Written by this ticket. Index of the four audit and acceptance bundles. |
| [`docs/reports/cli-audit-2026-09-08/README.md`](../reports/cli-audit-2026-09-08/README.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/create-reproductions.json`](../reports/cli-audit-2026-09-08/create-reproductions.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/created-issues.json`](../reports/cli-audit-2026-09-08/created-issues.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/dispatch-checks.json`](../reports/cli-audit-2026-09-08/dispatch-checks.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/epic-filing.md`](../reports/cli-audit-2026-09-08/epic-filing.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/epic-graph.json`](../reports/cli-audit-2026-09-08/epic-graph.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/epic-parent-create-result.json`](../reports/cli-audit-2026-09-08/epic-parent-create-result.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/epic-proposal.json`](../reports/cli-audit-2026-09-08/epic-proposal.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/graph-create-error.json`](../reports/cli-audit-2026-09-08/graph-create-error.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/inventory.json`](../reports/cli-audit-2026-09-08/inventory.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/inventory.md`](../reports/cli-audit-2026-09-08/inventory.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/live-checks.json`](../reports/cli-audit-2026-09-08/live-checks.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/proposal-commit-error.json`](../reports/cli-audit-2026-09-08/proposal-commit-error.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/proposal-create-result.json`](../reports/cli-audit-2026-09-08/proposal-create-result.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/cli-audit-2026-09-08/test-results.json`](../reports/cli-audit-2026-09-08/test-results.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-reliability-2026-09-08/acceptance.md`](../reports/integration-reliability-2026-09-08/acceptance.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-reliability-2026-09-08/activity-window-20260909-0854.json`](../reports/integration-reliability-2026-09-08/activity-window-20260909-0854.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-reliability-2026-09-08/matter-engine-plan.md`](../reports/integration-reliability-2026-09-08/matter-engine-plan.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-reliability-2026-09-08/matter-integration-policy.json`](../reports/integration-reliability-2026-09-08/matter-integration-policy.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-reliability-2026-09-08/matter-procedural.graph.json`](../reports/integration-reliability-2026-09-08/matter-procedural.graph.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-reliability-2026-09-08/matter-selection.graph.json`](../reports/integration-reliability-2026-09-08/matter-selection.graph.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-safeguards-2026-09-09/DELIVERY.md`](../reports/integration-safeguards-2026-09-09/DELIVERY.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-safeguards-2026-09-09/LIVE-SUMMARY.json`](../reports/integration-safeguards-2026-09-09/LIVE-SUMMARY.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-safeguards-2026-09-09/REVIEW.md`](../reports/integration-safeguards-2026-09-09/REVIEW.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/integration-safeguards-2026-09-09/SOURCE-INDEX.md`](../reports/integration-safeguards-2026-09-09/SOURCE-INDEX.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/unmerged-branches-2026-09-09/DISCORD-ACCEPTANCE.md`](../reports/unmerged-branches-2026-09-09/DISCORD-ACCEPTANCE.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/unmerged-branches-2026-09-09/EXECUTION.md`](../reports/unmerged-branches-2026-09-09/EXECUTION.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/unmerged-branches-2026-09-09/MERGE-PLAN.md`](../reports/unmerged-branches-2026-09-09/MERGE-PLAN.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/unmerged-branches-2026-09-09/README.md`](../reports/unmerged-branches-2026-09-09/README.md) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/unmerged-branches-2026-09-09/agent-queue-inventory.json`](../reports/unmerged-branches-2026-09-09/agent-queue-inventory.json) | `historical` | Audit evidence bundle; preserved unedited. |
| [`docs/reports/unmerged-branches-2026-09-09/merge-plan.json`](../reports/unmerged-branches-2026-09-09/merge-plan.json) | `historical` | Audit evidence bundle; preserved unedited. |

## `docs/reviews/` — review evidence

Immutable.

| Path | Disposition | Note |
|---|---|---|
| [`docs/reviews/2026-09-02-merge-sweep-fresh-delta-43.md`](../reviews/2026-09-02-merge-sweep-fresh-delta-43.md) | `historical` | Review evidence; preserved unedited. |
| [`docs/reviews/2026-09-02-review-azure-meadow-pr108.md`](../reviews/2026-09-02-review-azure-meadow-pr108.md) | `historical` | Review evidence; preserved unedited. |
| [`docs/reviews/2026-09-02-review-bright-meadow-pr98.md`](../reviews/2026-09-02-review-bright-meadow-pr98.md) | `historical` | Review evidence; preserved unedited. |
| [`docs/reviews/README.md`](../reviews/README.md) | `current` | Written by this ticket. Index of the three review write-ups. |

## `docs/gates/` — gate evidence

Immutable.

| Path | Disposition | Note |
|---|---|---|
| [`docs/gates/README.md`](../gates/README.md) | `current` | Written by this ticket. Index and banner; distinguishes a gate file from a live human gate. |
| [`docs/gates/wave1-1c-trust-ops.md`](../gates/wave1-1c-trust-ops.md) | `historical` | Gate acceptance evidence; preserved unedited. |

## `docs/plans/`

Plans for work, not descriptions of shipped behaviour.

| Path | Disposition | Note |
|---|---|---|
| [`docs/plans/README.md`](../plans/README.md) | `current` | Written by this ticket. Index separating in-flight plans from proposed ones. |
| [`docs/plans/documentation-overhaul/README.md`](../plans/documentation-overhaul/README.md) | `current` | This overhaul's planning material; owned by `foundation`. |
| [`docs/plans/documentation-overhaul/known-inaccuracies.md`](../plans/documentation-overhaul/known-inaccuracies.md) | `current` | This overhaul's planning material; owned by `foundation`. |
| [`docs/plans/documentation-overhaul/module-inventory.json`](../plans/documentation-overhaul/module-inventory.json) | `current` | This overhaul's planning material; owned by `foundation`. |
| [`docs/plans/documentation-overhaul/module-ownership.json`](../plans/documentation-overhaul/module-ownership.json) | `current` | This overhaul's planning material; owned by `foundation`. |
| [`docs/plans/documentation-overhaul/refresh_inventory.py`](../plans/documentation-overhaul/refresh_inventory.py) | `current` | This overhaul's planning material; owned by `foundation`. |
| [`docs/plans/documentation-overhaul/tasks.graph.json`](../plans/documentation-overhaul/tasks.graph.json) | `current` | This overhaul's planning material; owned by `foundation`. |
| [`docs/plans/install-onboarding/README.md`](../plans/install-onboarding/README.md) | `current` | Plan for proposed work (epic `noble-apex`); not a description of shipped behaviour. |
| [`docs/plans/install-onboarding/tasks.graph.json`](../plans/install-onboarding/tasks.graph.json) | `current` | Plan for proposed work (epic `noble-apex`); not a description of shipped behaviour. |

## `docs/playbooks/` — reviewed policy bundles

This repository's own configured policy, recorded as immutable reviewed bundles.

| Path | Disposition | Note |
|---|---|---|
| [`docs/playbooks/README.md`](../playbooks/README.md) | `current` | Written by this ticket. Index of this repository's reviewed policy bundles. |
| [`docs/playbooks/integration-only/default-pipeline/artifact.json`](../playbooks/integration-only/default-pipeline/artifact.json) | `current` | Reviewed playbook bundle recording this repository's own operator-requested policy. |
| [`docs/playbooks/integration-only/default-pipeline/artifact.sha256`](../playbooks/integration-only/default-pipeline/artifact.sha256) | `current` | Reviewed playbook bundle recording this repository's own operator-requested policy. |
| [`docs/playbooks/integration-only/default-pipeline/manifest.md`](../playbooks/integration-only/default-pipeline/manifest.md) | `current` | Reviewed playbook bundle recording this repository's own operator-requested policy. |
| [`docs/playbooks/integration-only/default-pipeline/source.md`](../playbooks/integration-only/default-pipeline/source.md) | `current` | Reviewed playbook bundle recording this repository's own operator-requested policy. |
| [`docs/playbooks/standard-high-default/default-assignment-routing/artifact.json`](../playbooks/standard-high-default/default-assignment-routing/artifact.json) | `current` | Reviewed playbook bundle recording this repository's own operator-requested policy. |
| [`docs/playbooks/standard-high-default/default-assignment-routing/artifact.sha256`](../playbooks/standard-high-default/default-assignment-routing/artifact.sha256) | `current` | Reviewed playbook bundle recording this repository's own operator-requested policy. |
| [`docs/playbooks/standard-high-default/default-assignment-routing/manifest.md`](../playbooks/standard-high-default/default-assignment-routing/manifest.md) | `current` | Reviewed playbook bundle recording this repository's own operator-requested policy. |
| [`docs/playbooks/standard-high-default/default-assignment-routing/source.md`](../playbooks/standard-high-default/default-assignment-routing/source.md) | `current` | Reviewed playbook bundle recording this repository's own operator-requested policy. |

## `docs/default_rules/` and `docs/example_playbooks/` — samples

Nothing installs or loads these. Written for the V1 runtime.

| Path | Disposition | Note |
|---|---|---|
| [`docs/default_rules/README.md`](../default_rules/README.md) | `historical` | Sample rule file, never installed by any vault or playbook installer. |
| [`docs/default_rules/dependency-update-check.md`](../default_rules/dependency-update-check.md) | `historical` | Sample rule file, never installed by any vault or playbook installer. |
| [`docs/default_rules/error-recovery-monitor.md`](../default_rules/error-recovery-monitor.md) | `historical` | Sample rule file, never installed by any vault or playbook installer. |
| [`docs/default_rules/periodic-project-review.md`](../default_rules/periodic-project-review.md) | `historical` | Sample rule file, never installed by any vault or playbook installer. |
| [`docs/default_rules/post-action-reflection.md`](../default_rules/post-action-reflection.md) | `historical` | Sample rule file, never installed by any vault or playbook installer. |
| [`docs/default_rules/proactive-codebase-inspector.md`](../default_rules/proactive-codebase-inspector.md) | `historical` | Sample rule file, never installed by any vault or playbook installer. |
| [`docs/default_rules/spec-drift-detector.md`](../default_rules/spec-drift-detector.md) | `historical` | Sample rule file, never installed by any vault or playbook installer. |
| [`docs/example_playbooks/README.md`](../example_playbooks/README.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/agent-queue/bugfix-pipeline.md`](../example_playbooks/agent-queue/bugfix-pipeline.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/agent-queue/exploration.md`](../example_playbooks/agent-queue/exploration.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/agent-queue/feature-pipeline.md`](../example_playbooks/agent-queue/feature-pipeline.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/agent-queue/log-analysis.md`](../example_playbooks/agent-queue/log-analysis.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/agent-queue/review-cycle.md`](../example_playbooks/agent-queue/review-cycle.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/codebase-inspector.md`](../example_playbooks/codebase-inspector.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/dependency-audit.md`](../example_playbooks/dependency-audit.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/system-health-check.md`](../example_playbooks/system-health-check.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/task-outcome.md`](../example_playbooks/task-outcome.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |
| [`docs/example_playbooks/vibecop-weekly-scan.md`](../example_playbooks/vibecop-weekly-scan.md) | `historical` | Playbook authoring sample written for the V1 runtime, which was deleted in the 2026-09-04 cutover. |

## `docs/img/`

| Path | Disposition | Note |
|---|---|---|
| [`docs/img/project-chat-00.png`](../img/project-chat-00.png) | `current` | Screenshot used by the landing pages. |
| [`docs/img/project-chat-01.png`](../img/project-chat-01.png) | `current` | Screenshot used by the landing pages. |
| [`docs/img/system-status-task-list.png`](../img/system-status-task-list.png) | `current` | Screenshot used by the landing pages. |
| [`docs/img/task-information.png`](../img/task-information.png) | `current` | Screenshot used by the landing pages. |
| [`docs/img/task-thread.png`](../img/task-thread.png) | `current` | Screenshot used by the landing pages. |

## `notes/` — working notes

Dated scratch material, never revised.

| Path | Disposition | Note |
|---|---|---|
| [`notes/README.md`](../../notes/README.md) | `current` | Written by this ticket. Dated index of the working notes. |
| [`notes/blocked-task-detection-procedure-2026-04-22.md`](../../notes/blocked-task-detection-procedure-2026-04-22.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/dependency-audit-2026-04-02.md`](../../notes/dependency-audit-2026-04-02.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/discord-ui-research.md`](../../notes/discord-ui-research.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/github-actions-integration.md`](../../notes/github-actions-integration.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/new-agent-profiles-research.md`](../../notes/new-agent-profiles-research.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/screenshot-analysis.md`](../../notes/screenshot-analysis.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/smart-forge-status.md`](../../notes/smart-forge-status.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/stuck-task-detection-procedure-2026-04-22.md`](../../notes/stuck-task-detection-procedure-2026-04-22.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/system-health-check-missing-tools-2026-04-22.md`](../../notes/system-health-check-missing-tools-2026-04-22.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/test-fix-plan.md`](../../notes/test-fix-plan.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/test-results-2026-03-26.md`](../../notes/test-results-2026-03-26.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/test-results-and-fix-plan.md`](../../notes/test-results-and-fix-plan.md) | `historical` | Working note kept with the repository; dated, never revised. |
| [`notes/test-run-findings-20260314.md`](../../notes/test-run-findings-20260314.md) | `historical` | Working note kept with the repository; dated, never revised. |

## `reports/` — root-level reports

Immutable.

| Path | Disposition | Note |
|---|---|---|
| [`reports/README.md`](../../reports/README.md) | `current` | Written by this ticket. Index of the two root-level reports. |
| [`reports/mcp-capabilities-workflow-2.md`](../../reports/mcp-capabilities-workflow-2.md) | `historical` | Working report; preserved unedited. |
| [`reports/merge-sweep-sharp-grove-56.md`](../../reports/merge-sweep-sharp-grove-56.md) | `historical` | Working report; preserved unedited. |

## `.superpowers/` — spec-driven-development evidence

Immutable per-task working record for two features.

| Path | Disposition | Note |
|---|---|---|
| [`.superpowers/README.md`](../../.superpowers/README.md) | `current` | Written by this ticket. Index of the spec-driven-development evidence. |
| [`.superpowers/sdd/2026-09-01-playbook-v2-engine-executors/task-14-report.md`](../../.superpowers/sdd/2026-09-01-playbook-v2-engine-executors/task-14-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/deferred-11b/test_integration_protection.py.txt`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/deferred-11b/test_integration_protection.py.txt) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/deferred-11b/test_integration_transport.py.txt`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/deferred-11b/test_integration_transport.py.txt) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/deferred-11b/transport.patch`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/deferred-11b/transport.patch) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/dev-toolchain-readiness.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/dev-toolchain-readiness.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/operational-scope-override.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/operational-scope-override.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/pre-final-hierarchy-audit.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/pre-final-hierarchy-audit.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/progress.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/progress.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-1-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-1-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-fix1-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-fix1-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-fix2-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-fix2-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10a-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-fix1-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-fix1-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-fix1-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-fix1-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-fix2-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-fix2-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10b-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-fix-1-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-fix-1-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-fix-2-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-fix-2-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-10c-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11-recovery-interface-map.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11-recovery-interface-map.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11-slices.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11-slices.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-fix-1-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-fix-1-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-fix-2-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-fix-2-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11b-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11b-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11b-provider-notes.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11b-provider-notes.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-entrypoints.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-entrypoints.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-fix-1-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-fix-1-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-fix-2-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-fix-2-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11c-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-cadence-amendment.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-cadence-amendment.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-fix-1-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-fix-1-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-operational-handoff.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-operational-handoff.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11d-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-12-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-12-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-12-copy-fixture-preflight.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-12-copy-fixture-preflight.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-2-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-2-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-5-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-5-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-6-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-6-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-7a-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-7a-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-7c-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-7c-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-8a-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-8a-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-8b-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-8b-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-fix-1-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-fix-1-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-fix-2-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-fix-2-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-fix-3-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-fix-3-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-rereview-1.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-rereview-1.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-rereview-2.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-rereview-2.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-rereview-3.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-rereview-3.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9a-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-1-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-1-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-2-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-2-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-3-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-3-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-4-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-4-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-5-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-5-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-6-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-fix-6-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-1.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-1.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-2.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-2.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-3.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-3.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-4.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-4.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-5.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-5.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-6.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-rereview-6.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b1-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round2-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round2-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round2-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round2-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round3-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round3-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round3-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round3-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round4-brief.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round4-brief.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round4-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round4-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round4-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1a-round4-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1b-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1b-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1b-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-fix-1b-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-report.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-report.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-review.md`](../../.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-9b2-review.md) | `historical` | Spec-driven-development working evidence; preserved unedited. |
| [`.superpowers/sdd/README.md`](../../.superpowers/sdd/README.md) | `current` | Written by this ticket. Per-bundle index and file-naming convention. |

## Contradictions this ledger retired

The overhaul's acceptance named six kinds of stale guidance. Where each one was,
and what now says otherwise:

| Retired claim | Where it was | Current answer |
|---|---|---|
| The default pipeline creates a reviewer for every completed task and gates downstream work on the verdict. | `docs/reviews/` framing, the root `README.md`, the shipped `aq-playbooks-and-gates` skill. | The shipped default pipeline has three rules — spec ingest, a proposal human gate, and batch commit on that gate resolving — and creates no reviewers. [Playbooks V2](../concepts/playbooks.md); [`docs/playbooks/integration-only/`](../playbooks/README.md) records this repository's own removal of task and branch reviews. |
| An unrouted task becomes a triage task that a human pins. | The root `README.md`. | The orchestrator emits `task.route_needed`; the shipped routing playbook chooses a class and profile and calls `task_route`. The triage shape is an **optional** routing-playbook shape a project may adopt. [Agents and routing](../concepts/agents-and-routing.md). |
| Delivery means squash-then-review, a pull request, hosted-CI receipts and a per-parent verifier. | `docs/guides/feature-merge-history.md`, `docs/guides/hierarchical-integration-trains.md`, `docs/guides/upgrade-integration-mode.md`. | Those are the **optional strict** modes, off by default. This repository runs [development integration](../guides/development-integration.md): ordinary branches, no squash, no PR, batched delivery. [Integration](../concepts/integration.md). |
| V1 playbooks, their compiler and their runner. | `docs/guides/playbook-v2-cutover-*.md`, `docs/example_playbooks/`, `docs/default_rules/`. | V1 was deleted in the 2026-09-04 cutover and `tests/test_v1_removal.py` is the ratchet. Authoring is source → propose → validate → activate. [Playbooks V2](../concepts/playbooks.md). |
| Discord as a control surface: slash commands, task controls, gate buttons, a bot token as a prerequisite. | `docs/guides/getting-started.md`, `docs/index.md`, `docs/specs/messaging/`. | Discord is notification-only — one channel, an activity digest, one thread per escalation — and `messaging_platform: none` is supported. [Messaging](../concepts/messaging.md). |
| SQLite as a storage backend. | `docs/index.md`, `docs/guides/hierarchical-integration-trains.md`, `docs/guides/e2e-swarm.md`. | PostgreSQL is the only backend; `tests/test_sqlite_removal.py` fails if a dialect branch returns. The one-way `aq db import-sqlite` importer refuses a non-empty target. |
| In-process agent runtimes with `start()` / `wait()` / `stop()`. | `docs/guides/runtime-development*.md`, `docs/specs/runtimes/`. | There are no in-tree runtimes. Every agent is an external CLI in a tmux session, selected by a profile's `harness` field. [Sessions](../concepts/sessions.md), [harness reference](../reference/harnesses.md). |
| A MkDocs Material site as a second publication format. | `mkdocs.yml`, whose `nav:` block lists 49 pages that do not exist. | GitHub-rendered Markdown with relative links is the publication format. The decision is recorded in `mkdocs.yml`'s own header; removing the Pages deploy touches `.github/workflows/docs.yml`, which this ticket does not own, and is tracked separately. |

## Related pages

* [Historical material](README.md) — how the preserved trees are organised.
* [The documentation map](../documentation-map.md) — the tree, page ownership and the shard rules.
* [Known inaccuracies](../plans/documentation-overhaul/known-inaccuracies.md) — contradictions recorded with evidence, including the ones this ticket could not fix because it does not own the file.
* [Documentation style](../contributing/documentation-style.md) — the rules a replacement page must follow so a retired claim does not come back.
