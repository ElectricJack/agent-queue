---
tags: [spec, runtime, acpx, acp]
---

# ACPXRuntime

Runtime that fans out to any [ACP](https://agentclientprotocol.com/protocol/overview)-compatible
coding agent via [openclaw/acpx](https://github.com/openclaw/acpx).

## Why one runtime for many agents

ACP normalises the wire shape across coding agents (Claude / Codex /
Gemini / OpenCode / Cursor / GitHub Copilot / Factory Droid / iFlow /
Kilocode / Kimi / Kiro / Qoder / Qwen / Trae — 14+ in ACPX's built-in
registry). Every agent speaks JSON-RPC 2.0 over NDJSON; the runtime
side stays one event-dispatch loop instead of one-per-agent.

Adding a new ACP agent later is a registry / profile change, not new
runtime code.

## Profile shape

Profiles selecting `ACPXRuntime` set both `runtime` and `agent_name`:

```json
{
  "runtime": "acpx",
  "agent_name": "claude"
}
```

`agent_name` is the ACP agent identifier — `"claude"` / `"codex"` /
`"gemini"` / `"opencode"` / `"cursor"` / etc. ACPX's registry resolves
the name to the agent's binary (e.g. `claude-acp`, `codex-acp`).

The parser (`src/profiles/parser.py:_validate_config`) rejects
`runtime: "acpx"` with empty or missing `agent_name` at sync time, so
the runtime can rely on a value being present in production.

## Subprocess invocation

```
acpx --format json --approve-all <agent_name> exec '<prompt>'
```

The shape is identical to `ClaudeCLIRuntime` / `CodexCLIRuntime`:

| Flag | Purpose |
|---|---|
| `--format json` | NDJSON output stream (one JSON event per line) |
| `--approve-all` | Auto-approve every tool call (orchestrator default) |
| `<agent_name>` | The ACP agent to dispatch to |
| `exec` | Single-task headless mode (vs interactive) |
| `<prompt>` | The full task prompt (positional) |
| `--model <id>` | Forwarded when `profile.model` is set |
| `--cd <path>` | Working directory — set from `TaskContext.checkout_path` |

Cancellation is cooperative SIGTERM through the existing
`src/runtimes/_subprocess.py` plumbing, with SIGKILL after the grace
window. ACPX itself maps SIGTERM to ACP `session/cancel` which the
underlying agent honors.

## Capabilities

In v1, `ACPXRuntime.capabilities = frozenset(Capability)` — the full
set. Per-agent tightening (e.g., declaring that the Codex agent
doesn't expose hooks) is a follow-up that consumes `set_config`
capability negotiation from ACP.

## Event dispatch

ACP events arrive as JSON-RPC envelopes. The runtime's `_dispatch()`
maps them to the orchestrator's `on_message` callback:

| Event | Surfaced as |
|---|---|
| `initialize` / `session/new` | Captures `sessionId`; no callback |
| `session/update` → `agent_message_chunk` | Streamed text → callback |
| `session/update` → `agent_thought_chunk` | Suppressed from live stream |
| `session/update` → `tool_call` | `-# {tool_name}` to callback |
| `session/update` → `plan_update`, `tool_result` | Logged only |
| Final response with `stopReason` | Builds `AgentOutput` |

`stopReason` values:

- `"completed"` → `AgentResult.COMPLETED`, summary = result text
- `"failed"` → `_classify_acp_error()` over the error text (rate-limit /
  quota → `PAUSED_*`; otherwise `FAILED`)
- `"cancelled"` → `AgentResult.FAILED` with `summary="Cancelled"`

Token usage is summed across the common ACP keys (`input_tokens`,
`output_tokens`, `prompt_tokens`, `completion_tokens`) so this works
across agents that report usage with different field names.

## Result extraction

Different ACP servers place `stopReason` either at the top level or
inside `params.result`:

```python
# Top-level shape
{"stopReason": "completed", "result": "Done.", "usage": {...}}

# Nested shape
{"method": "result", "params": {"result": {"stopReason": "completed", ...}}}
```

`_final_result_event()` accepts both — looks for the most-recent event
that carries a `stopReason`, regardless of nesting.

## Tradeoffs

**Pros:**

- Instant 14+ agent surface via ACP registry — adding a new agent is
  a profile change, not a runtime
- One dispatch loop maintains all ACP-compatible agents
- Future-proof against new coding agents joining the registry

**Cons:**

- Alpha-status dependency (ACPX) — version pinning + active
  maintenance attention required
- Error classification fidelity depends on what each ACP server
  surfaces; the heuristic in `_classify_acp_error()` works for common
  rate-limit / quota cases but won't catch every custom error
- Cooperative-only cancellation through ACP — `session/cancel` plus
  SIGTERM/SIGKILL fallback

## Sources

- [ACP Protocol Overview](https://agentclientprotocol.com/protocol/overview)
- [openclaw/acpx GitHub](https://github.com/openclaw/acpx)
- [ACPX Inside Claude Code: Practical Multi-Agent Orchestration](https://casys.ai/blog/acpx-multi-agent-orchestration)
- [ACP clients listing](https://agentclientprotocol.com/get-started/clients)
