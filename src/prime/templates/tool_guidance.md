This session's surface is CLI-first (docs/specs/design/aq-surface.md D6). Prefer shelling
out to `aq <command>` for anything exploratory or administrative — `aq task list`, `aq task
show <id>`, `aq schema` (never guess enum values; look them up), `aq doctor`, etc. The CLI
costs far less context than reasoning about a large tool schema, and it works identically
across harnesses.

A small, fixed set of native tools also exists for calls you'll make mid-turn where a native
tool call beats shelling out: `task_show`, `task_set`, `task_close`, `task_heartbeat`,
`task_claim`, `task_handoff`, `ask_human`, `message_send`, `message_inbox`, `memory_save`,
`memory_search`. Use whichever of the two paths (CLI or native tool) is more convenient —
both hit the same command handler and return identical results. `task_close`'s only two
outcomes are `pass` and `fail` (`aq schema`'s `outcome` enum). If you're missing context to
finish, say so in the summary and close `fail`.
