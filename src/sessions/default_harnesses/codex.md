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
  "supports_hooks": false,
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

**Luna requires a current Codex CLI.** The fast intelligence-class presets use
`gpt-5.6-luna` for Codex. This model was verified with Codex **0.151.0** using
ChatGPT login; **0.125.0** rejected it and requested a newer CLI. This records
versions tested, not an exact minimum version.

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

**`supports_hooks: false`.** Codex has no settings-file hook mechanism like
Claude's `--settings`, so there is no prompt-boundary `aq inbox --inject`
path. Queued messages reach a Codex session via nudge (keystrokes into the
pane) or transcript-tail fallback; task completion is unaffected because it
is explicit (`aq task close` through the injected CLI/MCP surface).

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

**Startup noise is harmless:** an update banner, a bubblewrap PATH warning,
and possible MCP-startup warnings all render above the composer and need no
keys. The `login-required` dialog quarantines instead of typing — an
unauthenticated codex cannot be fixed by keystrokes; run `codex login` on
the host.
