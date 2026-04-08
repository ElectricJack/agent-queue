---
tags: [overview, index]
---

# Agent Queue

**Put your AI agents to work. Go touch grass.**

If you're on Claude Max (or any subsidized plan with a big token budget), you're probably leaving most of it on the table. The tokens reset every few hours whether you used them or not. The throttle lifts at 3am and there's nobody at the keyboard. You finish a task, alt-tab away for ten minutes, and the agent sits idle. That's the real cost — not the tokens you spend, but the ones you waste by not having work queued up.

Agent Queue is a task queue and orchestrator built specifically around this constraint. It keeps one or more Claude Code agents busy across all your projects, automatically recovers from rate limits, and queues the next task before the current one finishes. When the throttle window resets, work resumes immediately — whether you're awake or not.

You manage everything from Discord on your phone. Queue up a week's worth of tasks before you leave the house. Come back to a stack of completed PRs.

<table>
<tr>
<td><img src="img/project-chat-00.png" alt="Chatting with the bot — status, suggestions, agent management" width="450"></td>
<td><img src="img/project-chat-01.png" alt="Task started and completed — token usage, change summary" width="450"></td>
</tr>
</table>

## How it works

Agent Queue runs as a background daemon. You talk to it through a dedicated Discord channel — just type naturally, like you're texting a dev lead who has root access to your machine. A Supervisor (LLM-powered conversation interface) understands context, remembers what you were working on, and acts.

![Agent working in a task thread — reading code, fixing bugs, running tests, committing](img/task-thread.png)

## Features

- **Parallel agents.** Multiple Claude Code agents work simultaneously across projects, each in its own workspace with your existing environment (`.env`, `venv`, `node_modules`).
- **Full task lifecycle.** Created → assigned → branched → worked → tested → completed. Retries on failure, escalates when stuck, never silently drops work.
- **Live streaming.** Each task gets a Discord thread. Watch agents work in real time. Reply to unblock them.
- **Rate limit recovery.** When an agent hits the throttle, the task auto-pauses. When the window resets, it auto-resumes. While one agent is throttled, others keep working.
- **Proportional scheduling.** Weight projects by priority. The scheduler distributes work fairly across a rolling window.
- **Hooks.** Automated triggers (periodic or event-driven) that gather context and invoke the LLM with tools — self-healing test suites, log analyzers, post-task reviewers.
- **Project notes.** Per-project markdown knowledge bases, readable and writable by both you and your agents.
- **Link your repos.** Point at existing directories, clone remotes, or init new ones. No setup overhead.
- **Token tracking.** Per-project and per-task usage breakdowns, visible from Discord.
- **Crash recovery.** SQLite-backed state. Survives restarts. Dead agents detected, tasks rescheduled, timers resumed.
- **Multi-provider.** Anthropic direct, AWS Bedrock, Google Vertex AI, or Ollama.
- **Zero orchestration overhead.** No LLM calls for scheduling. Every token goes to agent work.

![System status and task tree — agents, progress, queued work at a glance](img/system-status-task-list.png)

## Why Agent Queue?

- **Built for throttled plans.** Auto-pauses on rate limits, auto-resumes when the window resets. Works overnight, works while you're out, works while you sleep.
- **Development-specific.** Git branches, test verification, merge conflict handling. Not calendar automation.
- **Zero orchestration overhead.** No LLM calls to decide what to work on next. Every token goes to your agents.
- **Lightweight.** One Python process, SQLite. Runs on a Raspberry Pi. No Redis, no Kubernetes.
- **You're in control.** Nothing merges, nothing deploys without you seeing it. Discord notifications keep you in the loop from your phone.

## Getting started

### Prerequisites

- Python 3.12+
- A Discord bot token ([create one here](https://discord.com/developers/applications))
- Claude Code installed and configured

### Install & setup

```bash
git clone https://github.com/ElectricJack/agent-queue.git
cd agent-queue
./setup.sh
```

The setup script installs dependencies and walks you through Discord configuration, API keys, and getting your first agent running.

### First steps in Discord

Once the bot is online, everything happens through conversation in your control channel:

```
You:  link ~/code/my-app as my-app
Bot:  ✓ Linked. Repo "my-app" registered.

You:  create a project called my-app
Bot:  ✓ Project my-app created.

You:  create agent claude-1 and assign it to my-app
Bot:  ✓ Agent claude-1 created.

You:  add a task to add rate limiting to the API
Bot:  Created task `task-1` — "Add rate limiting to API"
      Assigned to claude-1. I'll post updates in the thread.
```

## Next steps

**Guides:**
- [[guides/getting-started|Getting Started]] — Installation and setup
- [[guides/discord-commands|Discord Commands]] — Slash commands and chat interactions
- [[guides/architecture|Architecture]] — How the system is designed
- [[guides/cli|CLI]] — Terminal interface reference
- [[guides/agent-tools|Agent Tools]] — Internal tool reference for AI agents
- [[guides/adapter-development|Adapter Development]] — Adding new agent backends

**Specifications:**
- [[specs/design/README|Design Specs]] — Next-generation architecture and guiding principles
- [[specs/orchestrator|Orchestrator]] — Core task and agent lifecycle
- [[specs/supervisor|Supervisor]] — LLM conversation loop
- [[specs/models-and-state-machine|Models & State Machine]] — Task lifecycle states

## License

MIT
