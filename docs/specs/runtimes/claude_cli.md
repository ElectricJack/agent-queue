---
tags: [spec, platforms, claude_cli]
---

# Claude CLI Platform

Wraps `claude -p --output-format stream-json --verbose` via subprocess. Functionally
equivalent to ClaudeSDKPlatform in v1 (the SDK is itself a wrapper around this CLI).

Implementation: `src/runtimes/claude_cli.py`. Shared subprocess helpers at
`src/runtimes/_subprocess.py`.

Capability set: `frozenset(Capability)` — same as the SDK runtime in v1.

## Verified event schema (claude v2.1.116)

```jsonl
{"type":"system","subtype":"hook_started","hook_id":"...","session_id":"..."}
{"type":"system","subtype":"hook_response","hook_id":"...","output":"..."}
{"type":"system","subtype":"init","session_id":"...","tools":[...],"model":"..."}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"..."}],"usage":{...}}}
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",...}}
{"type":"result","subtype":"success","is_error":false,"result":"...","usage":{...}}
```

Token accounting: `usage.input_tokens + usage.output_tokens` (matches existing
ClaudeSDKPlatform behavior — cache token fields are not summed).

(Detailed behavioral spec to be expanded as the runtime matures.)
