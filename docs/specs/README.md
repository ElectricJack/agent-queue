# Design specifications (historical)

<!-- aq:historical -->
> **Design record — not current documentation.** Everything in this directory
> states what was intended when it was approved. Specs are written before the
> code and are not revised to track it, so where a spec and the code disagree,
> the code is right. For what AQ does today start at
> [the documentation home](../README.md).

These files are preserved because they are the argument behind the system's
shape: why the command handler is the only write path, why playbooks replaced
hooks and rules, why sessions are external CLIs, why the vault is markdown.
Reading one tells you what problem a subsystem was built to solve. It does not
tell you what the subsystem does now.

## How this directory is organised

| Directory | What it holds |
|---|---|
| [`design/`](design/README.md) | Cross-cutting design specs: principles, playbooks, memory, coordination, the work graph, the surface, trust and ops. The [design index](design/README.md) is the best entry point. |
| [`implementation/`](implementation/README.md) | The implementation plan matching a design spec — packages, order, acceptance. |
| [`messaging/`](messaging/README.md) | The pre-simplification messaging platform specs. Both are superseded; Telegram support was deleted. |
| [`runtimes/`](runtimes/README.md) | Specs for the in-process runtime layer. **No runtimes ship**; every agent is a CLI in a tmux session. |
| this directory | One spec per subsystem, from the first build-out. |

`.obsidian/` holds vault settings so `docs/specs/` can be browsed in Obsidian.
AQ does not read it.

## Reading a spec safely

* Check the **Status** line at the top. "Draft", "approved direction" and a date
  are all common; none of them mean "shipped".
* Treat file paths in a spec as the paths that existed when it was written.
  Several subsystems have since been renamed or deleted — the in-process
  Supervisor, the V1 playbook compiler and runner, the Telegram adapter, the
  `acpx` runtime.
* Where a spec and a page under [`docs/concepts/`](../concepts/) disagree, the
  concept page was written against current source and wins.

## The current pages that replaced these

| If you opened a spec about | Read instead |
|---|---|
| The daemon's shape | [System architecture](../concepts/architecture.md) |
| Tasks, the work graph, gates | [Tasks, epics, dependencies and task graphs](../concepts/tasks.md) |
| Scheduling, pools, budgets | [Scheduling, worker pools and resource limits](../concepts/scheduling.md) |
| Profiles, routing, intelligence | [Agents and routing](../concepts/agents-and-routing.md) |
| Sessions, harnesses, runtimes | [Sessions](../concepts/sessions.md) |
| Playbooks, hooks, rules | [Playbooks V2](../concepts/playbooks.md) |
| Messaging, Discord, escalations | [Messaging, digests and escalations](../concepts/messaging.md) |
| Delivery, branches, merges | [Integration](../concepts/integration.md) |
| Commands and the CLI surface | [CLI reference](../reference/cli/README.md) |
| Provider and token accounting | [Providers, models and token accounting](../concepts/providers.md) |

Every file here has a recorded disposition in
[the disposition ledger](../history/disposition-ledger.md).
