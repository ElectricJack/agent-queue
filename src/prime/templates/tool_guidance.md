This session's surface is CLI-first (docs/specs/design/aq-surface.md D6). Prefer shelling
out to `aq <command>` for anything exploratory or administrative — `aq task list`, `aq task
show <id>`, `aq schema` (never guess enum values; look them up), `aq doctor`, etc. The CLI
costs far less context than reasoning about a large tool schema, and it works identically
across harnesses.

Run tests with `aq test <pytest args>` rather than bare `pytest` for anything past a single
file. It takes one of this box's global test slots before running, and applies the per-session
worker cap and the default marker deselects; everything that is not an `--aq-*` option is passed
to pytest untouched. A `waiting for 1 of N test slot(s)` line means the box is busy, not that you
are stuck; exit code 75 means no slot came free, which is retryable and not a test failure. Never
raise `-n` above what the session was given — the cap is what keeps concurrent agents from
saturating the machine.

A small, fixed set of native tools also exists for calls you'll make mid-turn where a native
tool call beats shelling out: `task_show`, `task_set`, `task_comment`, `task_comments`, `task_close`, `task_heartbeat`,
`task_claim`, `task_handoff`, `message_send`, `message_inbox`, `memory_save`,
`memory_search`. Use whichever of the two paths (CLI or native tool) is more convenient —
both hit the same command handler and return identical results. The ask_human command is not
available in this build; report blockers with message_send to the user. `task_close`'s only two
outcomes are `pass` and `fail` (`aq schema`'s `outcome` enum). If you're missing context to
finish, say so in the summary and close `fail`.
