---
id: fast-high
name: "Fast · High"
description: "Cheap small model — mechanical edits, single-file changes, well-known recipes. Thinking: extra-high reasoning."
tier: fast
thinking: xhigh
---

```json
{
  "anthropic": {"model": "claude-sonnet-5", "thinking": "xhigh"},
  "openai":    {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
  "codex":     {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
  "google":    {"model": "gemini-2.5-flash",    "thinking_budget": 24576}
}
```

The `-high` classes spend the *most* reasoning the provider offers, not merely
a lot of it: Anthropic takes `CLAUDE_CODE_EFFORT_LEVEL=xhigh` and Codex takes
`model_reasoning_effort="xhigh"`.
