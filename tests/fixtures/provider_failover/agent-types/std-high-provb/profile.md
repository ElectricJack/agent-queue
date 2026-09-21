---
id: std-high-provb
name: "E2E std-high-provb"
tags: [profile, agent-type, e2e, pool, provider-failover]
---

# E2E std-high-provb

## Role
A hand-authored pool worker for the provider-failover end-to-end kit: class
`std-high` on the fake `provb` harness.  `WORKER_PROVIDERS` is a fixed tuple, so
the kit authors these rather than deriving rungs -- which also proves an
operator-authored profile is recognised as an equivalent rung and fails
over.  Under Tier 1 nothing reads this Role: the smoke runner is the worker.

## Config
```json
{
  "harness": "provb",
  "lifecycle": "pool",
  "default_class": "std-high",
  "min_active": 0,
  "max_active": 1,
  "max_claims_per_session": 1,
  "needs_workspace": true,
  "workspaces": ["project-repo"]
}
```

## Tools
```json
{
  "allowed": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
}
```

## MCP Servers
```json
[]
```
