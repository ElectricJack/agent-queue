---
tags: [architecture, overview]
---

# Architecture — moved

<!-- aq:redirect -->
> **This page moved.** The current architecture page is
> [System architecture](../concepts/architecture.md). This stub is kept so
> existing links resolve.

The old page described an in-process runtime layer and a supervisor loop that no
longer exist. The replacement explains the daemon's real service boundaries:
the orchestrator cycle, the command handler as the single write path, sessions
as external CLIs under tmux, and the API and dashboard as readers of the same
state.

| You were looking for | Read |
|---|---|
| Processes, startup and service boundaries | [System architecture](../concepts/architecture.md) |
| How a task becomes a running agent | [Sessions](../concepts/sessions.md) |
| Which module does what | [Module catalog](../reference/modules/README.md) |
| The design specs this page was drawn from | [Historical material](../history/README.md) |
