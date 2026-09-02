---
id: gemini
name: Google Gemini CLI
tags: [harness, session-runtime]
---

# Google Gemini CLI

Runs the `gemini` CLI as a full interactive TUI (same rationale as the
claude/codex harnesses: attachable, observable, never a blocking
non-interactive `-p` prompt).

Edit this file to change how Gemini is launched. It is read live by the
vault watcher; no restart, no release.

## Config

```json
{
  "command": "gemini",
  "args": [],
  "prompt_mode": "arg",
  "permission_flag": "--yolo",
  "model_flag": "-m",
  "session_id_flag": "--session-id",
  "resume": {
    "style": "none"
  },
  "ready_delay_ms": 3000,
  "ready_prompt_prefix": "> ",
  "process_names": ["gemini", "node"],
  "skip_escape_before_enter": true,
  "composer_clear_keys": [
    "C-u"
  ],
  "supports_hooks": false,
  "instructions_file": "GEMINI.md",
  "dialogs": [
    {
      "name": "trust-folder",
      "pattern": "Do you trust the (files|contents) (in|of) this",
      "is_regex": true,
      "keys": ["Enter"]
    },
    {
      "name": "theme-select",
      "pattern": "Select Theme|Choose a theme",
      "is_regex": true,
      "keys": ["Enter"]
    },
    {
      "name": "auth-select",
      "pattern": "How would you like to authenticate|Select Auth Method",
      "is_regex": true,
      "keys": ["Enter"]
    },
    {
      "name": "login-required",
      "pattern": "Please sign in|authentication required|not authenticated",
      "is_regex": true,
      "keys": [],
      "quarantine": true
    }
  ]
}
```

## Notes

**`ready_prompt_prefix` is `>` + a plain space.** Gemini's composer prompt
prefix is a plain ASCII `>` followed by U+0020, distinct from Claude's
`❯` (U+276F) + NBSP and Codex's `›` (U+203A) + space. If you retype the
line, do not "fix" it to a fancier character.

**`resume.style` is `none`, deliberately.** Gemini's `--resume` takes an
integer *index* or the literal `latest` — not a UUID. The daemon's session
UUID cannot be used to pin a resume target the way `--session-id` pins a
fresh session id, so declaring resume-by-uuid would silently fall through
to a wrong session. `--session-id <uuid>` on a *fresh* launch is honored
(see `session_id_flag`), which is enough for the daemon to correlate
transcript files to session rows on the initial start; restarts start
fresh until a Gemini transcript reader lands (mirrors the codex.md
situation).

**`supports_hooks: false`.** Gemini has a `gemini hooks` subcommand for
its own tool-call hooks, but no settings-file mechanism analogous to
Claude's `--settings` that would let us inject `aq inbox --inject` at
every prompt boundary. Queued messages reach a Gemini session via nudge
(keystrokes into the pane) or transcript-tail fallback; task completion
is unaffected because it is explicit (`aq task close` through the
injected CLI/MCP surface).

**No `transcript_paths`.** Gemini writes session data under
`~/.gemini/tmp/<hash>/` and `~/.config/gemini-cli/` (snap: under the
snap's `$HOME`), keyed in a way no reader exists for yet. Listing a glob
without a reader would imply support the daemon does not have; pane
capture is the observation path.

**`permission_flag` is `--yolo` and follows the trust argument in
claude.md:** it is emitted only when the session's `work_dir` is an
isolated worktree (`RepoSourceType.WORKTREE`) or the profile explicitly
sets `permission_mode: bypassPermissions`. Gemini also has softer modes
(`--approval-mode auto_edit`) that could ride `args` if you want
prompt-approved edits with tool auto-approval. In a linked checkout the
flag is withheld and Gemini keeps its own approval prompts; the session
is attachable, so a human can answer them.

**Snap confinement warning is harmless:** the
`WARNING: cannot start document portal: dial unix /run/user/1000/bus:
connect: no such file or directory` line comes from snap's confinement
layer (the CLI is packaged as a snap), not from Gemini itself. It
renders above the composer and needs no keys. Similar startup noise
(update banner, MCP warnings) is also harmless.

**`skip_escape_before_enter: true`.** Gemini submits cleanly on Enter;
Escape in its composer clears input, so a blind Escape-then-Enter
sequence would eat the nudge text — the same reason it is set in
claude.md and codex.md.

**`login-required` quarantines instead of typing** — an
unauthenticated gemini cannot be fixed by keystrokes; run
`gemini` interactively once on the host to complete OAuth or set a
`GEMINI_API_KEY` in the daemon environment.

**`composer_clear_keys: ["C-u"]`** is the recovery key for a nudge that was
typed but never submitted. Enter races the composer's repaint (an attached
dashboard terminal resizing the pane is the reliable way to lose one), and
text left behind blocks every later nudge on the empty-composer guard — the
stall ladder then stops climbing forever. The provider first re-presses
Enter on a widening backoff; only if that still fails does it clear with
these keys, and only while it can still see its own marker on the input
line. Empty this list to make the provider leave the text alone instead.
