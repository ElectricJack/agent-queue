# How-to guides

A guide walks you through something you are trying to *do*. If you want to
understand a mechanism instead, read a [concept page](../concepts/); if you want
an exhaustive field-by-field list, read the [reference](../reference/).

New to AQ? Start with [Install](../tutorials/install.md) and
[Your first task](../tutorials/first-task.md), not with this directory.

## Current guides

| Guide | Use it when |
|---|---|
| [Project onboarding](project-onboarding.md) | Linking, initialising or cloning a repository into AQ. |
| [Setting up LLM providers](llm-providers.md) | Adding credentials, choosing models, setting quotas and budgets. |
| [Development integration](development-integration.md) | Turning on batched delivery and watching a batch reach your default branch. This is the delivery path **this repository** uses. |
| [Switching integration modes](integration-migration.md) | Moving a project into development mode, back out, or on to a strict mode. |
| [Integration troubleshooting](integration-troubleshooting.md) | A finished branch is not reaching your default branch. |
| [Troubleshooting worker sessions](session-troubleshooting.md) | A worker is running but nothing is happening, or it stopped and left something behind. |
| [Worker pools](worker-pools.md) | Operating `lifecycle: pool` profiles — bounds, quarantine, doctor checks, cutover. |
| [Resource gating](resource-gating.md) | Keeping N concurrent agents from taking the machine down. |
| [Default tuning](default-tuning.md) | The resource-aware defaults a fresh install gets, why each value, and how to override. |
| [Install, move defaults, or recover AQ](../tutorials/install.md#platform-quickstarts) | Choose the supported Windows/WSL2 or macOS path; export/import portable policy; repair, upgrade, or safely inspect uninstall. |
| [Escalations and the hourly digest](escalations.md) | Configuring the one Discord channel and answering escalation threads. |
| [Migration policy](migrations.md) | Who may run Alembic, against which database, and what to do when refused. |
| [CI at integration boundaries](integration-ci-boundaries.md) | Understanding which pushes launch the full test suite. |
| [End-to-end testing the swarm](e2e-swarm.md) | Proving claims, pools, formulas or the task hierarchy still compose, against a real daemon. |
| [Feature history and integration merges](feature-merge-history.md) | Reading ancestry in the **optional strict** integration modes. |
| [Hierarchical integration trains](hierarchical-integration-trains.md) | Rolling out the **optional, off-by-default** train mode for a project. |

Guides for the dashboard, plugins and MCP, and day-to-day operations are part of
this documentation overhaul and are added here by the tickets that own them; the
authoritative tree is [the documentation map](../documentation-map.md).

## Retired guides

These are kept only so existing links resolve. Each one carries a banner saying
what replaced it, and each has an entry in
[the disposition ledger](../history/disposition-ledger.md).

| Retired page | Read instead |
|---|---|
| [`getting-started.md`](getting-started.md) | [Install](../tutorials/install.md), [Your first task](../tutorials/first-task.md) |
| [`architecture.md`](architecture.md) | [System architecture](../concepts/architecture.md) |
| [`cli.md`](cli.md) | [CLI reference](../reference/cli/README.md) |
| [`agent-tools.md`](agent-tools.md) | [Agent-facing tools](../reference/cli/agent-tools.md) |
| [`task-state-machine.md`](task-state-machine.md) | [Tasks](../concepts/tasks.md) |
| [`discord-commands.md`](discord-commands.md) | [Messaging](../concepts/messaging.md), [Escalations](escalations.md) |
| [`runtime-development.md`](runtime-development.md), [`runtime-development-guide.md`](runtime-development-guide.md) | [System architecture](../concepts/architecture.md), [harness reference](../reference/harnesses.md) — there are no in-tree runtimes |
| [`discord-migration.md`](discord-migration.md), [`discord-replacement-checklist.md`](discord-replacement-checklist.md) | [Messaging](../concepts/messaging.md) — the cutover completed 2026-09-08 |
| [`playbook-v2-cutover-runbook.md`](playbook-v2-cutover-runbook.md), [`playbook-v2-cutover-report-template.md`](playbook-v2-cutover-report-template.md) | [Playbooks V2](../concepts/playbooks.md) — V1 was deleted 2026-09-04 |
| [`upgrade-integration-mode.md`](upgrade-integration-mode.md) | [Database migrations](../reference/database/migrations.md) |
| [`merge-gating.md`](merge-gating.md) | [CI at integration boundaries](integration-ci-boundaries.md) |
