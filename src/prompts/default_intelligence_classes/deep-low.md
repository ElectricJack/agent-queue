---
id: deep-low
name: "Deep · Low"
description: "Flagship model — cross-cutting design, architectural judgment, subtle bugs. Thinking: minimal reasoning."
tier: deep
thinking: low
---

```json
{
  "anthropic": {"model": "claude-fable-5", "thinking": "low"},
  "openai":    {"model": "gpt-5.6-sol", "reasoning_effort": "low"},
  "codex":     {"model": "gpt-5.6-sol", "reasoning_effort": "low"}
}
```

There is no `google` slice from the deep tier upward: no Gemini model belongs
in this bracket, and a missing slice resolves to *no model* rather than a
quiet downgrade. A `gemini`-harness profile therefore belongs on `fast-*` or
`standard-*`.
