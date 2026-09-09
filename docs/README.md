# Agent Queue documentation

Agent Queue (**AQ**) is a background service that runs AI coding agents against
your Git repositories. You describe work as *tasks*; AQ decides what is ready,
gives each task an isolated Git worktree, starts a coding-agent CLI inside a
terminal session, and carries the finished branch back into your repository.
You watch and steer it from a web dashboard, the `aq` command line, or an MCP
client.

This page is the entry point for every AQ document. It is organised as a
reading order, not an alphabetical list: sections near the top assume nothing,
sections near the bottom assume you have read the ones above.

> **Status.** The documentation set is being rewritten
> ([plan](plans/documentation-overhaul/README.md)). Pages marked **planned**
> below do not exist yet; the [documentation map](documentation-map.md) names
> the ticket that owns each one. Until a page lands, the closest existing
> material is under [Historical and existing material](#historical-and-existing-material)
> — read it knowing that parts of it describe behaviour AQ no longer has, which
> the [known-inaccuracies ledger](plans/documentation-overhaul/known-inaccuracies.md)
> records.

## Start here

New to AQ? Read in this order. Each step takes you further from "what is this"
and closer to "I run this every day".

| # | Page | What you get |
|---|---|---|
| 1 | [Glossary](reference/glossary.md) | The twenty words the rest of the documentation uses without explaining. Skim it now, come back to it often. |
| 2 | `docs/tutorials/install.md` — **planned** | Prerequisites, PostgreSQL, provider credentials, first start and shutdown. |
| 3 | `docs/tutorials/first-task.md` — **planned** | Onboard a throwaway repository, create one task, watch a worker do it, read the result. |
| 4 | [Core concepts](#core-concepts) | Why the system did what you just watched it do. |

## First task

The one-paragraph version of what steps 2 and 3 above will walk you through:
you install AQ and point it at a PostgreSQL database, register a Git
repository as a *project*, and create a *task* describing a change. The
orchestrator marks the task ready once nothing blocks it, reserves a *worktree
slot* — an isolated checkout of your repository on its own branch — and starts
a *worker session*: a coding-agent CLI running inside a terminal, primed with
the task, the repository and the project's accumulated knowledge. The worker
commits to its own branch and closes the task with a summary. Integration
collects finished branches and publishes them. Nothing touches your default
branch without going through that path.

## Core concepts

Read these when you want to know why AQ behaves the way it does. Each page
explains the vocabulary, then the mechanism, then the failure modes.

| Page | Covers |
|---|---|
| `docs/concepts/architecture.md` — **planned** | Daemon startup, the orchestrator cycle, service boundaries, who owns which state. |
| `docs/concepts/tasks.md` — **planned** | Task states, hierarchies, dependencies, formulas, deliverables, the graph view. |
| `docs/concepts/scheduling.md` — **planned** | How a ready task becomes a running worker: pools, claims, capacity, resource limits. |
| `docs/concepts/agents-and-routing.md` — **planned** | Agent profiles, intelligence classes, harnesses, and how a task is routed to one. |
| `docs/concepts/sessions.md` — **planned** | Sessions, attempts, claims and epoch fencing; what survives a restart. |
| `docs/concepts/projects-and-workspaces.md` — **planned** | Projects, repositories, workspace kinds, worktree slots, task branches. |
| `docs/concepts/integration.md` — **planned** | Finishing work versus delivering it; validation, publication and recovery. |
| `docs/concepts/playbooks.md` — **planned** | Playbooks V2: events, rules, gates, activation, and what ships enabled. |
| `docs/concepts/configuration-and-vault.md` — **planned** | Configuration file versus vault markdown versus database state. |
| `docs/concepts/providers.md` — **planned** | LLM providers, model selection, token accounting and budgets. |
| [Messaging, digests and escalations](concepts/messaging.md) | Messages to and from workers, the activity digest, escalation threads. |

## How-to guides

Task-shaped instructions for something you are trying to get done. The
[`guides/`](guides/) directory holds today's guides; several are accurate and
several predate the current design — see the ledger linked at the top of this
page before trusting one.

| Page | Covers |
|---|---|
| `docs/guides/dashboard.md` — **planned** | A tour of the dashboard, page by page, with the labels the UI actually uses. |
| `docs/guides/plugins-and-mcp.md` — **planned** | Installing plugins, configuring MCP servers, writing an extension. |
| `docs/guides/operations.md` — **planned** | Symptom-to-command troubleshooting and recovery runbooks. |
| [Worker pools](guides/worker-pools.md) | Operating the pull-based worker fleet. |
| [Resource gating](guides/resource-gating.md) | Test slots, per-session CPU and memory caps. |
| [Escalations and the hourly digest](guides/escalations.md) | Configuring the one Discord channel, reading the digest, answering an escalation. |
| [Migrations](guides/migrations.md) | Who may run Alembic against which database. |

## Reference and module catalog

Look-up material: exhaustive, terse, and generated from source where it can be.

| Page | Covers |
|---|---|
| [Glossary](reference/glossary.md) | Every term the documentation uses as jargon. |
| [Module catalog](reference/modules/README.md) | Every production module, its purpose and its owning page. |
| [CLI reference](reference/cli/README.md) | Every `aq` command group, its flags and its exit semantics. |
| `docs/reference/api/README.md` — **planned** | REST endpoints, WebSocket events and the two generated clients. |
| `docs/reference/configuration.md` — **planned** | Every configuration key, its default and when it is read. |
| `docs/reference/database/README.md` — **planned** | Tables, query modules and data lifecycle. |
| [CLI command inventory](reference/cli-command-inventory.md) | Generated list of the current command surface. |

## Contributing

| Page | Covers |
|---|---|
| [Documentation style](contributing/documentation-style.md) | How to write a page here, and the rules every runnable example must pass. |
| [Documentation map](documentation-map.md) | Which page owns which subject, and the ownership rules that keep two authors out of the same file. |
| `docs/contributing/setup.md` — **planned** | Getting a development checkout running. |
| `docs/contributing/testing.md` — **planned** | Running the focused tests for what you changed. |
| `docs/contributing/repo-map.md` — **planned** | Where things live in the repository. |

## Historical and existing material

AQ has been through several designs. The evidence is kept rather than deleted,
because it explains why the current design is shaped the way it is — but it is
**not** a description of current behaviour.

| Directory | What it is |
|---|---|
| [`docs/specs/`](specs/) | Design and implementation specifications, including superseded ones. |
| [`docs/superpowers/`](superpowers/) | Dated design and implementation specs for individual features. |
| [`docs/reports/`](reports/), [`docs/reviews/`](reviews/), [`docs/analysis/`](analysis/) | Point-in-time audits and reviews. |
| [`docs/plans/`](plans/) | Work plans, including [this documentation overhaul](plans/documentation-overhaul/README.md). |

`docs/history/README.md` — **planned** — will index this material with the
disposition of every page.

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
