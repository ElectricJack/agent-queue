# Agent Queue documentation

Agent Queue (**AQ**) runs coding-agent CLIs against Git repositories while it
keeps tasks, workspaces, sessions, and delivery records durable in PostgreSQL.
This is the documentation home: begin with the tutorial path, then use the
concepts, guides, and reference pages when you need more detail. The repository
[README](../README.md) links here from GitHub’s front page.

## Start here

New to AQ? Read these in order. Each page defines the terms it introduces and
links to the source and focused checks behind its claims.

| # | Page | What you learn |
| --- | --- | --- |
| 1 | [Glossary](reference/glossary.md) | AQ’s durable-state, agent, and delivery vocabulary. |
| 2 | [Install and start AQ](tutorials/install.md) | [Windows + WSL2](tutorials/install.md#windows-wsl2-quickstart) and [macOS](tutorials/install.md#macos-quickstart) quickstarts; prerequisites, PostgreSQL, a harness credential, first startup, and recovery. |
| 3 | [Run your first isolated task](tutorials/first-task.md) | Create a disposable project, follow one task, inspect its result, and clean up. |
| 4 | [Tasks](concepts/tasks.md) and [scheduling](concepts/scheduling.md) | Why AQ changes task state and when a worker can start. |
| 5 | [Integration](concepts/integration.md) and [operations](guides/operations.md) | The distinction between task completion and delivery, plus recovery paths. |

The newcomer example is deliberately disposable. It installs AQ, onboards a
new repository, creates a task, monitors it, reads its durable result, and
explains why a closed task is not automatically proof that a branch reached
`main`. It also names the common failures—missing project root, unavailable
profile, interrupted onboarding—and their recovery commands.

## Core concepts

Concept pages explain purpose, vocabulary, state ownership, and failure modes
before contributor internals.

| Page | Covers |
| --- | --- |
| [Architecture](concepts/architecture.md) | Daemon startup, the orchestrator cycle, services, and state boundaries. |
| [Tasks](concepts/tasks.md) | Lifecycle, hierarchy, dependencies, formulas, deliverables, and graph layout. |
| [Scheduling](concepts/scheduling.md) | Readiness, pools, capacity, and resource limits. |
| [Agents and routing](concepts/agents-and-routing.md) | Profiles, intelligence classes, harnesses, and assignment. |
| [Sessions](concepts/sessions.md) | Attempts, terminals, claims, epochs, and recovery. |
| [Projects and workspaces](concepts/projects-and-workspaces.md) | Repositories, workspace kinds, worktrees, and task branches. |
| [Integration](concepts/integration.md) | Finished work versus published work, policy, and repair. |
| [Playbooks](concepts/playbooks.md) | V2 event graphs, gates, activation, and shipped content. |
| [Configuration and vault](concepts/configuration-and-vault.md) | Configured policy, editable Markdown, and database projections. |
| [Providers](concepts/providers.md) | Provider settings, model selection, usage, and budgets. |
| [Messaging](concepts/messaging.md) | Worker messages, the activity digest, and human escalation replies. |

## Guides

Guides are task-shaped instructions. They describe configured local policy as
local policy, not as a universal default.

| Need | Guide |
| --- | --- |
| Use the web UI | [Dashboard](guides/dashboard.md) |
| Create and manage a project | [Project onboarding](guides/project-onboarding.md) |
| Operate pull-based workers | [Worker pools](guides/worker-pools.md) |
| Keep tests from exhausting the host | [Resource gating](guides/resource-gating.md) |
| Configure plugins and MCP | [Plugins and MCP](guides/plugins-and-mcp.md) |
| Configure digests and answer an escalation | [Escalations](guides/escalations.md) |
| Diagnose a daemon, task, session, or delivery issue | [Operations](guides/operations.md) |
| Deliver development branches | [Development integration](guides/development-integration.md) |
| Understand database migration authority | [Migrations](guides/migrations.md) |

## Reference

The [reference index](reference/README.md) is the entry point for exhaustive
look-up material. Its most-used pages are:

| Page | Covers |
| --- | --- |
| [Module catalog](reference/modules/README.md) | Every production source module, its purpose, component page, and test pointer. |
| [CLI reference](reference/cli/README.md) | Current `aq` command groups, contracts, agent tools, and prime documents. |
| [HTTP API](reference/api/README.md) | REST, WebSocket, and generated Python/TypeScript clients. |
| [Configuration](reference/configuration.md) | Settings, defaults, and reload boundaries. |
| [Database](reference/database/README.md) | PostgreSQL tables, queries, migrations, and lifecycle. |
| [Profiles and intelligence classes](reference/profiles-and-classes.md) | Runtime role configuration and model-selection policy. |

## Contributing

For a development checkout, begin with [Contributing](contributing/README.md).
Use [local checks](contributing/checks.md) to choose a focused test command,
[documentation style](contributing/documentation-style.md) for GitHub Markdown
rules, and [repository map](contributing/repo-map.md) to find code and its
catalog entry.

## Historical material

**Start here:** [Historical material](history/README.md) explains what is kept,
why, and how to read a historical page without being misled. Every documentation
file's disposition — `current`, `update`, `redirect`, `archive` or `historical` —
is recorded in [the disposition ledger](history/disposition-ledger.md).

| Directory | What it is |
|---|---|
| [`docs/specs/`](specs/README.md) | Design and implementation specifications, including superseded ones. |
| [`docs/superpowers/`](superpowers/README.md) | Dated design and implementation specs for individual features. |
| [`docs/reports/`](reports/README.md), [`docs/reviews/`](reviews/README.md), [`docs/analysis/`](analysis/README.md) | Point-in-time audits and reviews. |
| [`docs/plans/`](plans/README.md) | Work plans, including [this documentation overhaul](plans/documentation-overhaul/README.md). |
| [`notes/`](../notes/README.md), [`reports/`](../reports/README.md), [`.superpowers/`](../.superpowers/README.md) | Working notes and per-task evidence kept at the repository root. |

Retired guides keep their old paths and carry a banner naming the replacement;
[the guides index](guides/README.md) lists which are which.

## Conventions used on every page

* Everything is GitHub-rendered Markdown with relative links. There is no
  documentation site to build.
* A statement about behaviour names the module or command it came from, so you
  can check it.
* Shipped defaults, configured local policy, optional compatibility modes and
  proposed work are labelled as such and never blurred together.
* Commands are shown as you would type them, with the output you should expect.

See [documentation style](contributing/documentation-style.md) for the full
rules.

## Documentation maintenance

The [final coverage and disposition report](plans/documentation-overhaul/final-coverage-report.md)
records the local inventory, link, catalog, and focused-test checks used for
this navigation assembly. [Reference maintenance](reference/reference-maintenance.md)
explains how contributors repeat those checks after a source or documentation
change.
