# `aq prime` and the session hooks

The four commands a worker session runs that no operator ever types: `aq
prime` (what am I doing?), `aq handoff` (remember this before my context is
compacted), `aq inbox` (has anyone written to me?) and `aq subagent event`
(telemetry from the harness). They are the startup and lifecycle half of the
CLI, and they are why a worker can be dropped into a fresh worktree and still
know everything it needs.

Read [the CLI contract](README.md) for the rules these commands obey, and
[command groups](commands.md) for everything else on the surface.

## `aq prime`

`aq prime` prints one markdown document: the complete startup context for one
task. Role, rules, task, spec sections, workspace, pending messages, tool
guidance and the completion protocol — assembled in a fixed order, from the
vault and the database, with no LLM call and no writes.

```bash
aq prime
```

Inside a session no flags are needed: the task comes from `AQ_TASK_ID` and the
daemon URL and bearer token from `AQ_API_URL` / `AQ_API_TOKEN`. Outside one,
pass `--task-id`; `--session-id` and `--work-dir` are optional overrides.

### Two consumers, one renderer

The document is built by [`src/prime/`](../../../src/prime/), and two things
render it:

1. **`aq prime`** — the CLI command, backed by the `prime` daemon command
   ([`src/commands/surface_commands.py`](../../../src/commands/surface_commands.py)).
2. **The session prompt-file writer** — the session runtime calls
   `PrimeRenderer` in-process and writes the same body to
   `<work_dir>/.aq/prompt.md` before the harness launches, so a fresh worker's
   first prompt already contains it.

That is why a fresh start does *not* also run the hook: it would deliver the
same body twice. See [suppression](#hook-modes-and-suppression).

### Sections, in order

All ten slots always exist. A slot with nothing to say renders as an empty
string and is omitted from the assembled markdown — but it is still available
as a template variable to a [project override](#per-project-override-aqprimemd).

| # | Section | Source |
|---|---|---|
| 1 | Role | `vault/agent-types/<profile-id>/profile.md` — the `## Role` body, then `## Rules` under a `### Rules` sub-heading. |
| 2 | Project Role Override | `vault/projects/<pid>/agent-types/<profile-id>/profile.md`, same headings. It *supplements* section 1 rather than replacing it. |
| 3 | Task | Id, title, status and description of the task, plus review-deliverable and integration-delivery summaries when they apply. |
| 4 | Task Context | `task_context` rows, including `spec_ref` sections — spec text is inlined, not linked — and attachments. |
| 5 | Workspaces | `work_dir`, `branch`, `pr_url` and any other attached workspace kinds. |
| 6 | Messages | Pending messages plus the latest handoff note. |
| 7 | Facts | *Reserved for L1 memory. Renders empty while the memory subsystem is paused (`memory.enabled` defaults to false).* |
| 8 | Topic Context | *Reserved for L2 memory. Same.* |
| 9 | Tool Guidance | Static template, [`src/prime/templates/tool_guidance.md`](../../../src/prime/templates/tool_guidance.md). |
| 10 | Completion Protocol | Static template, [`completion_protocol.md`](../../../src/prime/templates/completion_protocol.md), with the task id substituted. |

Only the *prime-visible* profile headings reach the agent. `## Config`,
`## Tools`, `## MCP Servers`, `## Capabilities` and `## Reflection` stay
machine-only. For a session-launched agent this is the **only** channel its
rules travel on, which is why an empty `## Rules` block is a real problem
rather than a cosmetic one.

Section 10 is assembled, not fixed:

* A **pool** session (`lifecycle: pool`) gets
  [`completion_protocol_pool.md`](../../../src/prime/templates/completion_protocol_pool.md)
  appended — the close-and-claim-next loop, which is a materially different
  contract from a pushed task.
* A project in **development delivery** mode has the squash-before-review
  block replaced with the development-delivery paragraph.
* The [emergent-work](../../../src/prime/templates/emergent_work.md)
  instruction is appended **only if the profile's capability policy actually
  allows `create_task`**. Prime asks the same question the dispatch gate will
  answer, so an agent is never told to file work its own policy would then
  deny. The check fails *open* on anything unresolvable — an unknown profile
  is not a reason to drop a long-standing instruction.

Section 6 is the one section with a side effect. When prime is asked to mark
messages delivered, each rendered message is marked with `via="prime"` by
compare-and-set — that is what makes prime a genuine delivery method rather
than a peek. It reads three inboxes (task, profile, session), merges and
de-duplicates them, and orders them by priority then age. The messages
sub-section is gated on `messages.enabled`; the handoff note renders either
way.

### Per-project override: `.aq/PRIME.md`

If `<work_dir>/.aq/PRIME.md` exists — committed to the project repository, so
it rides into every worktree — its body **replaces the default assembly
entirely**. It is a Mustache-*style* template: literal `{{token}}`
substitution, with no sections, loops or partials.

Available variables: one per section key — `{{role}}`, `{{project_role}}`,
`{{task}}`, `{{task_context}}`, `{{workspaces}}`, `{{messages}}`,
`{{l1_facts}}`, `{{l2_context}}`, `{{tool_guidance}}`,
`{{completion_protocol}}` — plus `{{task.id}}`, `{{work_dir}}` and
`{{branch}}`. An unknown variable resolves to an empty string rather than
raising, so an override that names a slot this version does not have degrades
instead of breaking startup.

This is the escape hatch: a project that wants a different startup contract
edits a markdown file, not Python. It is also a loaded gun — an override that
drops `{{completion_protocol}}` produces workers that do not know how to
close.

### Hook modes and suppression

| Invocation | Output |
|---|---|
| `aq prime` | The markdown body. |
| `aq prime --hook-json` | The Claude Code `SessionStart` envelope: `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<body>"}}`. |
| `aq prime --hook-format <harness>` | That harness's envelope. `claude` is the JSON above; a harness with no structured hook output gets plain text. |

**Suppression.** When `AQ_STARTUP_PROMPT_DELIVERED=1` is set *and* a hook mode
was requested, the body is suppressed — an empty `additionalContext`. The
session's bootstrap prompt already delivered `.aq/prompt.md`, and delivering it
again would waste exactly the tokens this design saves. Wrapping is pure
presentation
([`src/prime/hook_envelopes.py`](../../../src/prime/hook_envelopes.py)), so a
suppressed hook call answers without touching the daemon at all.

## The shipped hook files

A harness that declares `supports_hooks` gets hook files written into its
work directory at session start, from the templates in
[`src/prime/templates/hooks/`](../../../src/prime/templates/hooks/). What
ships today:

| Harness | Hook | Command |
|---|---|---|
| `claude` | `SessionStart` (matcher `resume\|compact`) | `aq prime --hook-json` |
| `claude` | `PreCompact` | `aq handoff --auto` |
| `claude` | `SubagentStart`, `SubagentStop` | `aq subagent event --hook-json` |
| `codex` | `SubagentStart`, `SubagentStop` | `aq subagent event --hook-json` |

Three deliberate absences:

* **No `Stop` hook.** Completion is explicit — `aq task close` then `aq
  session drain-ack`. A `Stop` hook would re-introduce exit-as-success, which
  is the failure the session runtime exists to remove.
* **`SessionStart` matches `resume|compact` only.** A fresh start already
  receives the bootstrap prompt through argv; the hook is active exactly where
  that prompt is absent — resuming a session, and returning from a compaction.
* **No `UserPromptSubmit` hook** (removed 2026-08-27). It ran `aq inbox
  --inject` at every prompt boundary and cost roughly 1.3 s of interpreter
  startup per prompt, for a delivery path the cascade's nudge already covers.
  `aq inbox --inject` remains a supported command; it is simply not wired into
  a hook any more.

## `aq handoff`

```bash
aq handoff "Ran the migration" "Blocked on the alembic head; see task comment 4."
```

Writes a `task_context(type=handoff)` row on the current task — subject,
detail, timestamp and session id. The next `aq prime` for that task renders it
verbatim in section 6. This is how work state survives a compaction or a
session restart.

`--auto` is the difference that matters:

| Form | Effect |
|---|---|
| `aq handoff --auto …` | Note only. **Never** requests a restart. This is the `PreCompact` form; restarting on every compaction loops forever. |
| `aq handoff …` | Note **plus** a restart request. It records intent; whether and how to recycle the session is the session runtime's decision. |

Task and session default from `AQ_TASK_ID` / `AQ_SESSION_ID`, and the command
is claim-fenced like the other worker mutators.

## `aq inbox --inject`

```bash
aq inbox --inject
```

Prints pending messages for this session's recipient as a plain-text injection
block and marks them delivered. It is **hook-safe by design**: no pending
messages, an unresolvable recipient, or a daemon that is not running all exit
`0` with no stdout. A broken daemon must never block an agent's next prompt,
and a traceback must never land in the prompt window.

Use `aq message inbox` when you want the interactive form that actually
surfaces errors. `aq inbox` is the top-level alias with hook semantics; the
two share the same `message_inbox` command.

## `aq subagent event`

```bash
aq subagent event --hook-json      # payload on stdin, from the harness hook
```

Records one native sub-agent start or stop against the calling session. With
`--hook-json` it parses the harness's `SubagentStart` / `SubagentStop` stdin
payload; without it, `--event` and `--subagent-id` are required.

The daemon binds the row to the session that owns the bearer token, never to a
session named in the payload — a worker can only write its own telemetry. Both
Claude Code and Codex ship the same field names, and `agent_id` is the
*child's* id on both, which is what makes a start pair with its stop.

Like the inbox hook, it runs on the agent's critical path: an unparseable
payload, a dead daemon or an expired token all print nothing and exit `0`.
Set `AQ_SUBAGENT_HOOK_DEBUG` to any value to print the swallowed reason to
stderr — that is the only lever between "silent by design" and "silently
broken".

## State ownership

| State | Written by | Where it lives |
|---|---|---|
| The prime document | Nobody — it is assembled per call | Not persisted. `<work_dir>/.aq/prompt.md` is a copy written once at session start. |
| Role and rules | An operator or a profile command | `vault/agent-types/<id>/profile.md`, and the project override path |
| Task context and spec sections | `task_set`, spec ingest, playbooks | `task_context` rows |
| Handoff note | `aq handoff` | `task_context` rows, `type=handoff` |
| Message delivery marks | `aq prime` (`via="prime"`) and `aq inbox --inject` | `messages` rows |
| Sub-agent counts | `aq subagent event` | Session telemetry rows |
| The startup override | The project's own repository | `<work_dir>/.aq/PRIME.md`, committed |

## Common failures and recovery

| Symptom | Diagnose with | What it means |
|---|---|---|
| `aq prime` prints nothing in a hook | `env \| grep AQ_STARTUP_PROMPT_DELIVERED` | Suppression is working: the body was already delivered through the bootstrap prompt. Run plain `aq prime` to see it. |
| `Task '<id>' not found` | `aq task show <task-id>` | The task id resolved from `--task-id` or `AQ_TASK_ID` does not exist. |
| The worker never saw its rules | Open `vault/agent-types/<profile-id>/profile.md` | Only `## Role` and `## Rules` reach prime. Content under any other heading is machine-only. |
| A worker was told to file emergent work and then denied | `aq agent get-profile --profile-id <id>` | The profile's `## Capabilities` omits `create_task`, but prime could not resolve the profile and failed open. |
| Startup context looks nothing like the sections above | `ls <work_dir>/.aq/PRIME.md` | A project override is in force and replaces the whole body. |
| Messages never arrive mid-turn | `aq message inbox`, then `aq message status <id>` | There is no `UserPromptSubmit` hook. Pending messages arrive at the next prime, or through the cascade's nudge when the session goes idle. |

## Related pages

* [The CLI contract](README.md) — global options, envelope, exit codes,
  authority.
* [Command groups](commands.md) — every other command on the surface.
* [Agent-facing tools](agent-tools.md) — the tool-shaped version of the same
  commands, and the skills shipped alongside them.
* [Module catalog: CLI](../modules/cli.md) — the modules behind this page.

## Source and tests

Renderer [`src/prime/renderer.py`](../../../src/prime/renderer.py); section
builders [`src/prime/sections.py`](../../../src/prime/sections.py); document
model [`src/prime/models.py`](../../../src/prime/models.py); override loading
[`src/prime/overrides.py`](../../../src/prime/overrides.py); hook wrapping and
sub-agent payload parsing
[`src/prime/hook_envelopes.py`](../../../src/prime/hook_envelopes.py); the CLI
commands [`src/cli/agent_surface.py`](../../../src/cli/agent_surface.py) and
[`src/cli/messages.py`](../../../src/cli/messages.py). Design:
[`docs/specs/design/aq-surface.md`](../../specs/design/aq-surface.md) §5–§6.

```bash
aq test tests/test_prime_renderer.py tests/test_prime_hook_envelopes.py \
        tests/test_prime_session_workspace.py tests/test_cli_agent_surface.py \
        tests/test_agent_subagents.py tests/test_guidance_docs.py
```
