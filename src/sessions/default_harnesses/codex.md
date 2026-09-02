---
id: codex
name: OpenAI Codex
tags: [harness, session-runtime]
---

# OpenAI Codex

Runs the `codex` CLI as a full interactive TUI (same rationale as the
claude harness: attachable, observable, never a blocking print mode).

Edit this file to change how Codex is launched. It is read live by the
vault watcher; no restart, no release.

## Config

```json
{
  "command": "codex",
  "args": [],
  "prompt_mode": "arg",
  "permission_flag": "--dangerously-bypass-approvals-and-sandbox",
  "model_flag": "-m",
  "resume": {
    "style": "none"
  },
  "ready_delay_ms": 3000,
  "ready_prompt_prefix": "› ",
  "process_names": ["codex"],
  "skip_escape_before_enter": true,
  "supports_hooks": true,
  "hook_files": {
    ".codex/hooks.json": "hooks/codex.json"
  },
  "hook_trust_flag": "--dangerously-bypass-hook-trust",
  "instructions_file": "AGENTS.md",
  "dialogs": [
    {
      "name": "trust-directory",
      "pattern": "Do you trust the contents of this directory",
      "keys": ["Enter"]
    },
    {
      "name": "login-required",
      "pattern": "Sign in with ChatGPT|codex login|log out and sign in again",
      "keys": [],
      "quarantine": true
    }
  ]
}
```

## Notes

**Use a current Codex CLI.** Intelligence classes select `gpt-5.6-luna` for
fast work, `gpt-5.6-terra` for standard work, and `gpt-5.6-sol` for deep work.
Their Off and Low levels both use Codex's lowest supported reasoning level,
`low`; Medium and High use the corresponding levels. Generic OpenAI API
mappings remain separate. All three models were verified with Codex **0.151.0**
using ChatGPT login. The older **0.125.0** CLI was tested only with Luna; it
rejected that model and requested a newer CLI. These are versions tested,
not an exact minimum version.

**`ready_prompt_prefix` is `›` (U+203A) + a plain space** — verified by
launching codex 0.125.0 under tmux and capturing the pane bytes
(`E2 80 BA 20`). This is a different character from Claude's `❯` (U+276F).

**`resume.style` is `none`, deliberately.** Codex chooses its own session
UUID and offers no way to pin it at launch (no `--session-id` analogue),
so the daemon's session UUID means nothing to `codex resume` — declaring
`subcommand` resume made relaunches die with "No saved session found with
ID <daemon-uuid>" (observed live, 2026-08-21). The CLI itself supports
`codex resume <uuid>` / `codex fork`.

**The blocker is now gone**: the transcript reader learns the real UUID off
disk and `TranscriptWatcher._learn_session_key` writes it to
`sessions.session_key` on the first poll, so a restart *does* have a key to
resume from. Flipping this to
`{"style": "subcommand", "subcommand": "resume", "supports_fork": true}`
needs one thing verified first — that a session killed mid-turn resumes
cleanly from a rollout written by the previous process — so it is left as a
deliberate next step rather than flipped untested. Until then restarts
start fresh.

**`supports_hooks: true` — discovery, not a flag** (verified live against
codex-cli 0.151.0 on 2026-09-01). Codex has no settings-file flag like
Claude's `--settings`; it discovers hooks from `~/.codex/hooks.json` and
`<cwd>/.codex/hooks.json`. `SessionSpecBuilder` therefore writes the
project-level file into the session's work_dir and passes no flag for it —
which is why `_compose_argv`'s "a written-but-unpointed hook file is a lie"
rule has a second branch (`hook_trust_flag`) rather than one.

**`hook_trust_flag` is load-bearing.** A discovered hook file does *not*
run until it has been trusted: Codex persists a per-file
`{enabled, trusted_hash}` and only grants it through an interactive
"Hooks need review / Trust all and continue" screen, which a headless
session can never answer. Project `trust_level = "trusted"` is **not**
enough — measured: a fully trusted scratch repo with a valid
`.codex/hooks.json` fired nothing until the flag was added.
`--dangerously-bypass-hook-trust` is Codex's own documented hatch
("Intended only for automation that already vets hook sources"), and this
daemon writes the file itself. It is emitted only alongside
`permission_flag`, so it inherits the isolated-worktree argument in
claude.md rather than widening it: in a linked checkout Codex keeps hook
review and the session simply reports no native subagent telemetry.

The events wired today are `SubagentStart` / `SubagentStop`, both of which
carry `session_id`, `turn_id`, `agent_id`, `agent_type`, `cwd`,
`hook_event_name` and `transcript_path` (Stop adds `agent_transcript_path`
and `last_assistant_message`) — enough to pair a child's start with its
stop by `agent_id`. Handlers **must** carry `"type": "command"`; omitting
it makes Codex log `failed to parse hooks config … missing field 'type'`
and silently run no hooks at all.

There is still no prompt-boundary hook, so queued messages reach a Codex
session via nudge (keystrokes into the pane) or transcript-tail fallback;
task completion is unaffected because it is explicit (`aq task close`
through the injected CLI/MCP surface).

**Transcripts are read** (2026-08-27). Codex records sessions under
`~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<uuid>.jsonl`, keyed by date
rather than work_dir, so `CodexTranscriptReader`
(`src/sessions/transcripts/codex.py`) resolves a session by reading
`session_meta.payload.cwd` out of each candidate's first line — newest
first, capped at 200 files. Once the watcher has learned the UUID (below)
resolution is a direct filename match and no scan happens.

The rollout records the same conversation twice: `event_msg` is the UI
event stream, `response_item` the model-facing record. The reader takes
text from `event_msg` and tool calls from `response_item`, which are
disjoint — taking both `message` channels double-counts every turn, and
`response_item.message` additionally carries the system-prompt and
`<environment_context>` frames. Token usage comes from
`event_msg/token_count` using `last_token_usage`, never the cumulative
`total_token_usage`.

**`permission_flag` follows the trust argument in claude.md:** it is
emitted only when the session's work_dir is an isolated worktree (or the
profile opts in). In a linked checkout Codex keeps its own sandbox +
approval prompts, and the session is attachable so a human can answer them.
Codex additionally has softer modes (`--full-auto`, `-s workspace-write`)
that could ride `args` if you want sandboxed-but-automatic instead.

**`skip_escape_before_enter: true` is load-bearing** — Escape in the Codex
composer backtracks/clears; a blind Escape-then-Enter sequence would eat
the nudge text.

**The trust screen is painted late, and its rows start with `›`.** Codex's
trust menu renders `› 1. Yes, continue` — the same prefix the readiness poll
looks for — and it can land *after* the first dismissal pass. Startup
therefore refuses readiness on any capture where a rule above matches, and
holds the final pass open for `sessions.dialog_settle_seconds` (default
1.5 s). Verified live on Codex 0.151 (task smart-orbit.7).

**Startup noise is harmless:** an update banner, a bubblewrap PATH warning,
and possible MCP-startup warnings all render above the composer and need no
keys. The `login-required` dialog quarantines instead of typing — an
unauthenticated codex cannot be fixed by keystrokes; run `codex login` on
the host.
