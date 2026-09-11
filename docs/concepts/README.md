# Core concepts

One page per mechanism, written for someone who has never used AQ. Each explains
the vocabulary first, then how the thing works, then where its state lives and
what to do when it breaks.

Read them in this order the first time:

1. [System architecture](architecture.md) — the daemon, its cycle, and the
   service boundaries everything else sits inside.
2. [Tasks, epics, dependencies and task graphs](tasks.md) — the durable unit of
   work, and how a plan becomes a graph.
3. [Scheduling, worker pools and resource limits](scheduling.md) — how AQ decides
   what runs now, and what stops it flooding the machine.
4. [Agents and routing](agents-and-routing.md) — who does a task, and how hard
   they think about it.
5. [Sessions](sessions.md) — one run of a coding-agent CLI, under tmux, with a
   claim proving what it may touch.
6. [Integration](integration.md) — how a finished branch becomes a commit on your
   default branch.
7. [Playbooks V2](playbooks.md) — event-driven automation, compiled from Markdown
   and activated deliberately.
8. [Messaging, digests and escalations](messaging.md) — how text moves in and
   out, and how a machine asks a human.
9. [Providers, models and token accounting](providers.md) — where the
   intelligence comes from and what it costs.
10. [Software-factory policy](factory-policy.md) — the normative admission,
    delivery and recovery contract every role instruction references.

Pages for projects and workspaces, and for configuration and the vault, are part
of this documentation overhaul and are added by the tickets that own them; the
authoritative tree is [the documentation map](../documentation-map.md).

Related: [the tutorials](../tutorials/README.md) to get running,
[the guides](../guides/README.md) for a specific task,
[the reference](../reference/) for exhaustive detail, and
[historical material](../history/README.md) for the design records these pages
replaced.
