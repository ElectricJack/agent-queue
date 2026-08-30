---
id: deep-off
name: "Deep · Off"
description: "Flagship model — cross-cutting design, architectural judgment, subtle bugs. Thinking: lowest supported reasoning."
tier: deep
thinking: off
---

```json
{
  "anthropic": {"model": "claude-fable-5", "thinking": "low"},
  "openai":    {"model": "gpt-5.6-sol", "reasoning_effort": "none"},
  "codex":     {"model": "gpt-5.6-sol", "reasoning_effort": "low"},
  "google":    {"model": "gemini-2.5-pro",    "thinking_budget": 0}
}
```

Fable 5 always uses thinking, so Deep Off selects `low` rather than disabling it.
