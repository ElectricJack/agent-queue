---
id: claude
name: Claude Code
tags: [harness, session-runtime]
---

# Claude Code

The default harness. Runs the `claude` CLI as a **full interactive TUI** —
never `claude -p` print mode — so a human can attach to the session and read
it, and so the daemon observes progress rather than blocking on a stream.

Edit this file to change how Claude is launched. It is read live by the
vault watcher; no restart, no release.

## Config

```json
{
  "command": "claude",
  "args": [],
  "prompt_mode": "arg",
  "permission_flag": "--dangerously-skip-permissions",
  "model_flag": "--model",
  "session_id_flag": "--session-id",
  "resume": {
    "style": "flag",
    "flag": "--resume",
    "supports_fork": true
  },
  "ready_delay_ms": 2000,
  "ready_prompt_prefix": "❯ ",
  "process_names": ["claude", "node"],
  "skip_escape_before_enter": true,
  "supports_hooks": true,
  "hook_files": {
    ".aq/hooks/claude.json": "hooks/claude.json"
  },
  "instructions_file": "CLAUDE.md",
  "transcript_paths": ["~/.claude/projects/{work_dir_slug}/*.jsonl"],
  "max_argv_prompt_bytes": 1024,
  "dialogs": [
    {
      "name": "trust-folder",
      "pattern": "Do you trust the files in this folder?",
      "keys": ["Enter"]
    },
    {
      "name": "theme",
      "pattern": "Choose the text style that looks best",
      "keys": ["Enter"]
    },
    {
      "name": "bypass-permissions",
      "pattern": "Bypass Permissions mode",
      "keys": ["Down", "Enter"]
    },
    {
      "name": "mcp-trust",
      "pattern": "New MCP server found",
      "keys": ["Enter"]
    },
    {
      "name": "rate-limit",
      "pattern": "approaching your usage limit",
      "keys": ["Escape"],
      "quarantine": true
    }
  ]
}
```

## Notes

**`ready_prompt_prefix` is a non-breaking space.** Claude's prompt is
`❯` + U+00A0, not `❯` + U+0020. The readiness poll normalizes NBSP before
matching, so either spelling in this file works — but if you retype the
line, do not "fix" the character.

**`permission_flag`** relies on the trust argument in
[[design/trust-and-ops]]: the agent runs inside a disposable worktree with
a scrubbed environment, so skipping in-session permission prompts trades a
prompt no human is present to answer for a blast radius that is already
bounded. Remove the flag to get interactive permission prompts back — the
session is attachable, so a human *can* answer them.

**`skip_escape_before_enter: true`** — Claude submits cleanly on Enter. Some
harnesses need an Escape first to leave a mode; grok's Escape *clears* the
input, and codex's double-Escape backtracks, which is why this is per-harness
data and not a blind key sequence in provider code.

**Dialogs share one budget** (`sessions.dialog_budget_seconds`, default 8 s)
across the whole table, not 8 s each. Nine per-dialog budgets is how the
Gas City runtime blew its start deadline.

**No Stop hook.** Completion is explicit — `aq task close …` then
`aq session drain-ack`. A Stop hook would re-introduce exit-as-success,
which is the failure this whole runtime exists to remove.
