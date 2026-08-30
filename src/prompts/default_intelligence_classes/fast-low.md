---
id: fast-low
name: "Fast · Low"
description: "Cheap small model — mechanical edits, single-file changes, well-known recipes. Thinking: minimal reasoning."
tier: fast
thinking: low
---

```json
{
  "anthropic": {"model": "claude-sonnet-5", "thinking": "low"},
  "openai":    {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
  "codex":     {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
  "google":    {"model": "gemini-2.5-flash",    "thinking_budget": 2048}
}
```
