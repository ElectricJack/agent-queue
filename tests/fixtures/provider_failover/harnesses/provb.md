---
id: provb
name: "Fake provider b"
tags: [harness, session-runtime, e2e, fake]
---

# Fake provider b

A harness that exists only for the provider-failover end-to-end kit
(`docs/specs/provider-failover.md` D23).  No `provb` binary is ever run:
the kit's daemon uses `sessions.provider: fake`, and whether a start
succeeds is read from `sessions.fake_script_file`
(`src/sessions/fake_script.py`).  The two quarantine dialogs below are
the ones the fake raises -- `login-required` (`signal: auth`, a logged
out CLI) and `usage-limit` (`signal: usage`, an exhausted account) --
declared the way a real harness declares them.

No vendor: the id-keyed slice fallback resolves its model from the
`provb` slice of an intelligence class, and its availability key is its id.

## Config

```json
{
  "command": "provb",
  "args": [],
  "prompt_mode": "arg",
  "model_flag": "--model",
  "ready_delay_ms": 0,
  "process_names": ["provb"],
  "dialogs": [
    {
      "name": "login-required",
      "pattern": "Please log in to provb",
      "keys": [],
      "quarantine": true,
      "signal": "auth"
    },
    {
      "name": "usage-limit",
      "pattern": "You've hit your provb usage limit",
      "keys": [],
      "quarantine": true,
      "signal": "usage"
    }
  ]
}
```
