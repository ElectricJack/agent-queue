---
id: astra-low
name: "Astra · Low"
description: "Astra — the strongest model available, above the deep tier. OpenAI only (Codex harness). Thinking: minimal reasoning."
tier: astra
thinking: low
---

```json
{
  "openai": {"model": "gpt-6-astra", "reasoning_effort": "low"},
  "codex":  {"model": "gpt-6-astra", "reasoning_effort": "low"}
}
```

Astra is an OpenAI model and has no counterpart on another provider, so this
class carries **only** the OpenAI/Codex slices. A `claude`- or
`gemini`-harness profile on `astra-*` resolves no launch model (a warning, and
the CLI's own default) — point it at `deep-*` instead.
