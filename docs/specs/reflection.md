---
tags: [spec, reflection, self-improvement]
---

# Reflection Engine Specification

The ReflectionEngine manages the [[supervisor|Supervisor]]'s action-reflect cycle.

> **Future evolution:** Reflection becomes a [[design/playbooks|playbook]]-driven process. See [[design/vault-and-memory]] Section 11 for the self-improvement loop.

## Class: ReflectionEngine (src/reflection.py)

### Constructor
- `ReflectionEngine(config: ReflectionConfig)`

### Depth Determination

| Trigger | full | moderate | minimal |
|---------|------|----------|---------|
| task.completed | deep | standard | light |
| task.failed | deep | standard | light |
| hook.failed | deep | standard | light |
| user.request | standard | light | light |
| hook.completed | standard | light | light |
| passive.observation | light | light | light |
| periodic.sweep | light | light | light |

Level "off" returns None for all triggers.

### Reflection Prompts

**Deep:** 5-question verification (intent, success, rules, memory, follow-up)
**Standard:** 2-question check (success, relevant rules)
**Light:** Memory update only

### Safety Controls

1. **max_depth** (default 3): Maximum nested reflection iterations
2. **per_cycle_token_cap** (default 10000): Per-cycle token limit
3. **hourly_token_circuit_breaker** (default 100000): Auto-downgrades to minimal

### Invariants
- Reflection failure never breaks the primary action
- Token ledger entries older than 1 hour are excluded from circuit breaker
- Circuit breaker tripped → should_reflect() returns False
