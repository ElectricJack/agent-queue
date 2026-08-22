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

**`ready_prompt_prefix` is `›` (U+203A) + a plain space** — verified by
launching codex 0.125.0 under tmux and capturing the pane bytes
(`E2 80 BA 20`). This is a different character from Claude's `❯` (U+276F).

**`resume.style` is `none`, deliberately.** Codex chooses its own session
UUID and offers no way to pin it at launch (no `--session-id` analogue),
so the daemon's session UUID means nothing to `codex resume` — declaring
`subcommand` resume made relaunches die with "No saved session found with
ID <daemon-uuid>" (observed live, 2026-08-21). The CLI itself supports
`codex resume <uuid>` / `codex fork`; flip this back to
`{"style": "subcommand", "subcommand": "resume", "supports_fork": true}`
once a Codex transcript reader (`src/sessions/transcripts/`) can learn the
real UUID from `~/.codex/sessions/`. Until then restarts start fresh.

**`supports_hooks: false`.** Codex has no settings-file hook mechanism like
Claude's `--settings`, so there is no prompt-boundary `aq inbox --inject`
path. Queued messages reach a Codex session via nudge (keystrokes into the
pane) or transcript-tail fallback; task completion is unaffected because it
is explicit (`aq task close` through the injected CLI/MCP surface).

**No `transcript_paths`.** Codex records sessions under
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, keyed by date rather than
work_dir, and no reader exists for the format. Listing the glob without a
reader would imply support the daemon does not have; pane capture is the
observation path.

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
