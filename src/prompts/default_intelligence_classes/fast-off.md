---
id: fast-off
name: "Fast · Off"
description: "Cheap small model — mechanical edits, single-file changes, well-known recipes. Thinking: no extended reasoning."
tier: fast
thinking: off
---

```json
{
  "anthropic": {"model": "claude-haiku-4-5", "thinking": "off"},
  "openai":    {"model": "gpt-5-mini",    "reasoning_effort": "minimal"},
  "codex":     {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
  "google":    {"model": "gemini-2.5-flash",    "thinking_budget": 0}
}
```
