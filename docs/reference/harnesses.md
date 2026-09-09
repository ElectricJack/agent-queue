# Harness reference

A **harness** is the description of one coding-agent command-line tool: how to
invoke it, how to hand it a prompt, how to resume it, what "ready" looks like,
which processes belong to it, and which startup dialogs to answer. A harness is
a markdown file in the vault — never a Python module. Adding a new CLI is
authoring a file.

This page is exhaustive reference. For what a session *is* and how it is
managed, read [Sessions](../concepts/sessions.md).

## Where harness files live

| Scope | Path | Wins over |
|---|---|---|
| Project | `vault/projects/<project-id>/harnesses/<id>.md` | system |
| System | `vault/harnesses/<id>.md` | — |

The file is the source of truth: there is no database table for harnesses
([`src/sessions/harness_registry.py`](../../src/sessions/harness_registry.py)).
The vault watcher reparses a changed file live, so edits take effect without a
daemon restart. A parse failure keeps the *previous* entry rather than dropping
it — a half-saved file in an editor must not take a harness offline mid-run.

A profile selects its harness by id, with the `## Config.harness` field of
`vault/agent-types/<id>/profile.md`. That field is the only selector: every
agent runs as a session, and the harness decides which CLI that session runs.

## File format

YAML frontmatter, a `## Config` fenced JSON block, and optional free-form
notes. The notes are not parsed.

````markdown
---
id: claude
name: Claude Code
tags: [harness]
---

## Config

```json
{
  "command": "claude",
  "prompt_mode": "arg",
  "process_names": ["claude", "node"],
  "ready_prompt_prefix": "❯ ",
  "resume": {"style": "flag", "flag": "--resume"}
}
```

## Notes

Free-form prose.
````

Validation is deterministic and refuses rather than guesses: a missing
`command`, an unknown `prompt_mode`, an unknown `resume.style`, `prompt_mode:
flag` without a `prompt_flag`, a non-object `env`, or a `base` chain longer
than one level are all errors. **Unknown keys are a warning, not an error**, so
a file authored against a newer daemon still loads
([`src/sessions/harness_parser.py`](../../src/sessions/harness_parser.py)).

Check a vault copy without a daemon restart:

```bash
aq doctor --check harness.drift
aq vault reset-harness --dry-run
```

## Config keys

### Invocation

| Key | Type | Default | Meaning |
|---|---|---|---|
| `command` | string | — | The executable. Required, unless inherited via `base`. |
| `args` | list of strings | `[]` | Operator-supplied arguments, kept in their declared position. |
| `env` | object | `{}` | Environment entries for this harness. Merged as *explicit* values, so they survive scrubbing. |
| `base` | string | — | Inherit from another harness. **Single level only** — a chain is rejected, so cycles are impossible by construction. |

### Prompt delivery

| Key | Type | Default | Meaning |
|---|---|---|---|
| `prompt_mode` | `arg` \| `flag` \| `none` | `arg` | How the bootstrap prompt reaches the CLI. |
| `prompt_flag` | string | — | Required when `prompt_mode` is `flag`. |
| `max_argv_prompt_bytes` | integer | `1024` | Above this, the prompt is written to a file and passed by path. |

The bootstrap prompt is deliberately tiny: who you are, where you are, and
"run `aq prime`". The full prompt is rendered to `<work_dir>/.aq/prompt.md` and
comes back through `aq prime`, so a launch never has to squeeze a large prompt
through a command line.

Above `max_argv_prompt_bytes` the prompt is written to `.aq/tmp/prompt-<name>.txt`
and the argv becomes a small `sh -c` wrapper that reads it back and `exec`s the
harness, so the command string stays constant-size however long the prompt is.
The default of 1 KiB exists because tmux's `new-session` command buffer is
roughly 2 KiB, and a truncated command is a launch that half-works.

### Flags the launcher may add

Each of these is *permission* to use a flag, not an instruction to always use
it. The launcher emits them only when the corresponding value resolves.

| Key | Emitted when |
|---|---|
| `model_flag` | the resolved intelligence class maps to a model for this harness |
| `effort_flag` | the class resolves a reasoning effort *and* the harness declares the flag |
| `tools_flag` | the profile declares a tool allowlist. A harness without this key cannot be restricted, and the launch says so once rather than silently ignoring the profile |
| `session_id_flag` | a fresh start (never with resume) — pins the CLI's own session id to AQ's, so the transcript reader finds the file without guessing |
| `settings_flag` | `hook_files` actually rendered. Without it the hook payload is written and never read |
| `hook_trust_flag` | hooks rendered **and** the launch already qualifies for `permission_flag` |
| `permission_flag` | see [permission flags](#permission-flags-and-the-trust-argument) below |

### Readiness and observation

| Key | Type | Default | Meaning |
|---|---|---|---|
| `ready_delay_ms` | integer | `0` | Grace before the readiness poll starts. |
| `ready_prompt_prefix` | string | — | The composer's prompt prefix, matched against the pane capture. |
| `process_names` | list of strings | `[]` | Which processes in the pane's subtree count as "the agent". |
| `transcript_paths` | list of strings | `[]` | Globs for the CLI's on-disk conversation log. `{work_dir_slug}` is substituted. |

> **Warning.** `ready_prompt_prefix` characters are exact and are not
> interchangeable: Claude uses `❯` (U+276F) followed by a **non-breaking**
> space, Codex uses `›` (U+203A) plus a plain space, and Gemini a plain ASCII
> `>` plus a space. If you retype one of these lines, do not "fix" the
> character.

### Input behaviour

| Key | Type | Default | Meaning |
|---|---|---|---|
| `skip_escape_before_enter` | bool | `true` | Whether a nudge may press Enter without an Escape first. |
| `composer_clear_keys` | list of tmux key names | `[]` | Keys that clear this composer, used to recover a nudge that was typed but never submitted. Empty means "no known clear sequence" and the provider leaves the text alone. |

Both are per-harness data rather than a blind key sequence in provider code,
because the same key means different things in different composers: Escape
clears the input in Gemini, backtracks in Codex, and is unnecessary in Claude.

### Hooks

| Key | Type | Default | Meaning |
|---|---|---|---|
| `supports_hooks` | bool | `false` | Whether this CLI can run AQ's hook payload at all. |
| `hook_files` | object | `{}` | `destination path -> packaged template name`, written into the work directory before launch. |
| `instructions_file` | string | — | The CLI's project instructions file (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`). |

A written-but-unpointed hook file is a lie, so the launcher will not claim
hooks are provisioned unless the argv genuinely activates them — either through
`settings_flag` (Claude) or `hook_trust_flag` (Codex). Whether a launch really
did that is recorded once on the session row (`sessions.hooks_provisioned`) and
never re-derived from a harness file that may have been edited since. That is
what lets sub-agent counts say "complete" rather than "unknown" — and say
"unknown" honestly for the sessions that lack it.

### Resume

```json
"resume": {"style": "flag", "flag": "--resume", "supports_fork": true}
```

| `style` | Shape | Notes |
|---|---|---|
| `flag` | `claude --resume <key>` | Placed after the other flags. |
| `subcommand` | `codex resume <key>` | Placed immediately after the command — a subcommand following a flag is a different CLI grammar. |
| `none` | — | Restarts start fresh. |

The resume key is the harness's own conversation id, stored on
`sessions.session_key`. Where the CLI lets AQ pin it (`session_id_flag`), it is
known at launch; where it does not, the transcript reader learns it off disk on
the first poll.

### Startup dialogs

`dialogs` is an ordered list of rules run against the live pane during startup
([`src/sessions/dialogs.py`](../../src/sessions/dialogs.py)):

```json
{
  "name": "trust-folder",
  "pattern": "Do you trust the files in this folder",
  "is_regex": false,
  "keys": ["Enter"],
  "quarantine": false,
  "once": true
}
```

| Field | Meaning |
|---|---|
| `name` | Identifier used in logs and events. |
| `pattern` | Substring, or a regex when `is_regex` is true. Matched against the pane capture. |
| `keys` | tmux `send-keys` arguments, sent in order, when the pattern matches. |
| `quarantine` | When true, matching is **terminal**: the keys answer "stop" and the session is quarantined instead of continuing startup. |
| `once` | Fire at most once per startup. |

Two rules govern the whole table:

* **One shared budget.** Every dismissal pass of one startup draws down the
  same `sessions.dialog_budget_seconds` clock (8 s by default) — not 8 s per
  dialog. Per-dialog budgets serially exceed the start deadline.
* **Quiet windows catch late paints.** A pass that returns the moment one
  capture shows no dialog is racing the TUI. Claude and Codex both paint their
  trust screen *after* the first frames, so a pass holds its "no dialog"
  verdict open for `sessions.dialog_settle_seconds` (1.5 s), re-arming that
  clock every time a rule fires. Readiness is only believed on a capture where
  no rule matches — which matters because both trust screens' highlighted rows
  begin with the same prefix the readiness poll looks for.

## Permission flags and the trust argument

`permission_flag` declares the CLI's "skip approval prompts" flag. Declaring it
is permission to use it, not a promise to. The launcher emits it only when one
of two things is true
([`skip_permissions_allowed`](../../src/sessions/spec.py)):

1. the session's work directory is an **isolated git worktree**, or
2. the profile explicitly opts in.

The reasoning is the bounded blast radius of a disposable worktree: skipping a
prompt no human is present to answer is a fair trade *only* where the damage is
already contained. In a linked checkout of your real repository the flag is
withheld and the CLI keeps its own prompts — and because the session is
attachable, a human can answer them.

| Opt-in | Applies to | Effect |
|---|---|---|
| `claude_dangerously_skip_permissions: true` | Claude profiles | adds `--dangerously-skip-permissions`. Rejected on non-Claude profiles. |
| `codex_full_auto: true` | Codex profiles | adds `--ask-for-approval on-request --sandbox workspace-write`. Keeps Codex's workspace sandbox. Rejected on non-Codex profiles. |
| `permission_mode: bypassPermissions` | any profile | Optional compatibility, still supported: selects the harness's own `permission_flag`. |

Flags are composed idempotently: if a profile opt-in and the harness's `args`
request the same flag, the launch carries it once, in the position `args` gave
it. A profile value of `false` never removes a flag an operator deliberately
placed in `args`. Where `codex_full_auto` and the stronger bypass would both
apply, the bypass wins and `--full-auto` is suppressed rather than sending
conflicting modes to the CLI.

> **Warning.** "Dangerously" is accurate. These modes let the CLI use tools
> without interactive approval. A disposable worktree limits where ordinary
> repository writes land; it does not prevent reads elsewhere on the host,
> writes through other tools, or network access. Use the opt-in only where the
> session environment, tool set and task-scoped credentials supply the
> boundary you need.

## The shipped harnesses

Three harness files ship with AQ in
[`src/sessions/default_harnesses/`](../../src/sessions/default_harnesses/) and
are copied into `vault/harnesses/` on first start. Once a vault copy exists it
is the source of truth; a copy you have edited is never overwritten by an
upgrade.

| | [claude](../../src/sessions/default_harnesses/claude.md) | [codex](../../src/sessions/default_harnesses/codex.md) | [gemini](../../src/sessions/default_harnesses/gemini.md) |
|---|---|---|---|
| CLI | `claude` | `codex` | `gemini` |
| Instructions file | `CLAUDE.md` | `AGENTS.md` | `GEMINI.md` |
| Prompt prefix | `❯` + NBSP | `›` + space | `>` + space |
| Ready delay | 2000 ms | 3000 ms | 3000 ms |
| Model flag | `--model` | `-m` | `-m` |
| Pin session id | `--session-id` | no | `--session-id` |
| Resume | `--resume`, forkable | **none** (deliberate) | **none** (deliberate) |
| Hooks | yes, via `--settings` | yes, by discovery + `--dangerously-bypass-hook-trust` | no |
| Transcript reader | yes | yes | no |
| Permission flag | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `--yolo` |
| Quarantining dialog | rate limit | login required | login required |

Each shipped file's `## Notes` section carries the measurement behind every row
above — which CLI version it was verified against, which byte sequence the
prompt prefix really is, and why a deliberate `none` is deliberate. Read the
file rather than trusting this table if the two ever disagree.

### Why two harnesses declare `resume: none`

Both are recorded limitations rather than missing work:

* **Codex** chooses its own session UUID and offers no way to pin one at
  launch, so AQ's session id means nothing to `codex resume`. Declaring
  subcommand resume made relaunches die with "No saved session found". The
  transcript reader now learns the real UUID off disk and writes it to
  `sessions.session_key`, so the blocker is gone — but flipping the switch
  needs one thing verified first (that a session killed mid-turn resumes
  cleanly from a rollout written by the previous process), so it is left as a
  deliberate next step rather than flipped untested.
* **Gemini's** `--resume` takes an integer index or the literal `latest`, not a
  UUID, so a UUID-shaped resume would silently target the wrong session.
  `--session-id` on a *fresh* launch is honoured, which is enough to correlate
  a transcript to a session row on the initial start.

Until either changes, a restart of those harnesses starts fresh.

## Transcripts

A transcript reader maps a CLI's own on-disk conversation record into
normalized entries ([`src/sessions/transcripts/`](../../src/sessions/transcripts/)).
It lives outside the provider because a provider knows how a session *runs*,
not how the harness records the conversation — the same Codex transcript is
readable whether the CLI ran under tmux, under a bare subprocess, or in your
own shell.

| Harness | Location | How a session is resolved |
|---|---|---|
| Claude | `~/.claude/projects/<slug>/<session-uuid>.jsonl`, slug derived from the working directory | direct path |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<uuid>.jsonl` | reads `session_meta.payload.cwd` from each candidate's first line, newest first, capped at 200 files — then by filename once the UUID is learned |
| Gemini | not read | pane capture is the observation path |

Gemini deliberately declares no `transcript_paths`: it writes session data in a
shape no reader exists for, and listing a glob without a reader would imply
support the daemon does not have.

Reads are byte-offset incremental, and the offset is mirrored durably keyed by
*transcript path* rather than by session id. That matters because a session
that dies and is relaunched on the same workspace resolves to the same file
under a new session id — an in-memory offset of zero would replay the entire
file, re-emitting every past turn and charging every past token a second time.

### What AQ derives from a transcript

* New entries become agent-output events, one per completed turn.
* Assistant entries carrying `usage` become exactly one token-usage row each,
  idempotent per entry rather than per line.
* Tail activity refreshes the session lease and the agent heartbeat, so a busy
  session does not climb the stall ladder.

Codex's rollout records the same conversation twice — an event stream and a
model-facing record. AQ takes text from one and tool calls from the other,
because they are disjoint; taking both message channels double-counts every
turn and drags in system-prompt frames.

> **Privacy.** AQ reads the CLI's own files on your disk and does not create a
> second copy of the conversation. What it stores is derived: normalized
> entries served over the session stream, token counts and read offsets.
> `aq session logs` and the session SSE stream both apply the same scope check,
> so a worker's task-scoped token cannot read another project's session. A
> harness with no reader falls back to a labelled peek of the visible screen —
> the source is always stated, because a silent switch is how an operator
> debugs the wrong thing.

## Platform differences

| | tmux | subprocess | fake |
|---|---|---|---|
| Platform | POSIX (Linux, WSL2, macOS) | any | any |
| Attach a terminal | yes | no | no |
| Peek the screen | yes | no | scripted |
| Nudge / send input | yes | no | scripted |
| Survives a daemon restart | yes — the tmux server owns it | the process does, but it cannot be re-adopted | n/a |
| Drain-ack durability | survives a restart (`tmux set-environment`) | lost on restart (daemon memory) | in memory |
| `confirm_stopped` | fresh, cache-bypassing probe | **not implemented** — cannot confirm | n/a |

The shipped default is `sessions.provider: subprocess`, because it is the
provider every host can construct; a default the registry cannot build would
fail every launch. **Configured local policy:** this install sets
`sessions.provider: tmux` in `~/.agent-queue/config.yaml`, which is what makes
attach, peek, nudge and adoption-after-restart work here.

Consequences of running on `subprocess`, all of them by design rather than
oversight: the stall ladder skips its nudge rungs and goes straight to restart
(there is nothing to nudge *with*), `list_running` enumerates only sessions the
current daemon process started, and the dashboard's terminal and live-pane
views are unavailable. Tasks still complete normally, because completion is
`aq task close` and not a stream.

`fake` is an in-memory provider, importable from production code on purpose:
`sessions.provider: fake` is a supported value, so a daemon can be brought up
on the full session path with nothing spawned. Every reconciler and cascade
test runs against it.

## Adding a harness

1. Write `vault/harnesses/<id>.md` with the frontmatter and `## Config` block
   above. Copy the closest shipped file and change what differs.
2. Verify the launch shape by hand — especially `ready_prompt_prefix` (capture
   the real bytes; do not guess the character) and the trust or login screens
   the CLI shows on a fresh directory.
3. Point a profile at it with `## Config.harness: <id>`.
4. Check it parses and the binary is reachable:
   ```bash
   aq doctor --check harness.drift
   aq doctor --check harness.binaries
   ```

You do not need to restart the daemon; the vault watcher picks the file up.

> **Note.** `harness.binaries` probes a fixed list — `git`, plus the shipped
> harness CLIs and `gh` — rather than deriving it from the vault, so a
> harness you add yourself will not appear in that check's output.

## Editing a shipped harness

Once a shipped harness has been copied into the vault, the vault copy wins.
AQ records the hash of every version of every shipped file it has ever
published ([`src/sessions/harness_manifest.py`](../../src/sessions/harness_manifest.py)),
so at startup it can tell three cases apart:

| Status | Meaning | What startup does |
|---|---|---|
| `missing` | no vault copy | creates it |
| `current` | byte-identical to the shipped file | nothing |
| `stale` | byte-identical to a *previously* shipped version | refreshes it in place — nobody edited it, so a fix reaches it |
| `edited` | anything else | leaves it alone and reports it |

To discard a local edit and take the shipped file back:

```bash
aq vault reset-harness --dry-run       # report every shipped harness's status
aq vault reset-harness claude
aq vault reset-harness --all
```

The running daemon picks the change up without a restart.

> **Configured local policy.** On this machine the `claude` and `codex` vault
> copies are operator-edited, so they read as `edited` and shipped fixes do not
> reach them automatically. `aq vault reset-harness` or a hand-applied patch is
> required for a shipped harness change to take effect here.

## Related pages

* [Sessions](../concepts/sessions.md) — what a harness file is used to launch,
  and how that launch is then managed.
* [Session troubleshooting](../guides/session-troubleshooting.md) — startup
  dialogs, quarantines and drift, as symptoms.
* [Terminals and claims](terminals-and-claims.md) — the pane and terminal
  surfaces that depend on a harness's input behaviour.
* [Module catalog: sessions](modules/sessions.md) — every module named on this
  page, one row each.

## Source and tests

Implementation:
[`src/sessions/harness_parser.py`](../../src/sessions/harness_parser.py),
[`src/sessions/harness_registry.py`](../../src/sessions/harness_registry.py),
[`src/sessions/harness_manifest.py`](../../src/sessions/harness_manifest.py),
[`src/sessions/spec.py`](../../src/sessions/spec.py),
[`src/sessions/dialogs.py`](../../src/sessions/dialogs.py),
[`src/sessions/transcripts/`](../../src/sessions/transcripts/).

```bash
aq test tests/test_harness_parser.py tests/test_harness_manifest.py tests/test_session_spec.py tests/test_transcript_readers.py
```
