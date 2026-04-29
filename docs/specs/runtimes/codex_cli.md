---
tags: [spec, platforms, codex_cli]
---

# Codex CLI Platform

Wraps `codex exec --json --skip-git-repo-check --sandbox workspace-write`.

Implementation: `src/runtimes/codex_cli.py`. Shared subprocess helpers at
`src/runtimes/_subprocess.py`.

Capability set: `frozenset({STREAMING_JSON, RESUME, THINKING, MCP, PLAN_MODE})`.
Codex doesn't share Claude's skills / MEMORY.md / hooks infrastructure, so those
capabilities are absent.

## Verified event schema (codex v0.125.0)

```jsonl
{"type":"thread.started","thread_id":"..."}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"...","type":"agent_message","text":"..."}}
{"type":"item.started","item":{"id":"...","type":"file_change","changes":[...],"status":"in_progress"}}
{"type":"item.completed","item":{"id":"...","type":"file_change","changes":[...],"status":"completed"}}
{"type":"item.started","item":{"id":"...","type":"command_execution","command":"...","status":"in_progress"}}
{"type":"item.completed","item":{"id":"...","type":"command_execution","aggregated_output":"...","exit_code":0,"status":"completed"}}
{"type":"turn.completed","usage":{"input_tokens":N,"cached_input_tokens":M,"output_tokens":K,"reasoning_output_tokens":J}}
```

Inline non-fatal: `{"type":"error","message":"..."}` (don't classify as failure).
Terminal failure: `{"type":"turn.failed","error":{"message":"..."}}`.

Token accounting: `input_tokens + output_tokens + reasoning_output_tokens`.
`cached_input_tokens` is informational (subset of input_tokens), NOT additive.

Summary on success: built from accumulated `agent_message` item texts (since
`turn.completed` has no summary field).

Always passes `--skip-git-repo-check` (agent-queue task workspaces aren't
always git repos) and `--sandbox workspace-write` (sane default for autonomous
agents).

(Detailed behavioral spec to be expanded as the runtime matures.)
