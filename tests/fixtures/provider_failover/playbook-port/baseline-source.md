---
id: provider-failover
name: Provider failover
version: 1
scope: system
enabled: true
triggers:
  - provider.state_changed
  - timer.5m
---

# Provider failover

When a provider runs out of usage or loses its login, the daemon stops
launching against it on its own: that is mechanism, and it needs no
playbook. Moving the work that was waiting for it somewhere else is policy,
and this playbook is that policy. It holds no state and makes no decision of
its own; the `provider_reroute` command owns the rules — the same class on
the next available provider or a hold, a pool-width of moves at a time, a
record of every move on the task — and a project that wants other rules
keeps a project-scope copy that passes different arguments. The design is
`docs/specs/provider-failover.md` (D11).

If this playbook is not active, tasks whose provider is unavailable hold with
a visible reason and nothing moves. That is the safe failure, and
`aq doctor --check providers.failover_playbook` reports it.

## Rule: reroute-on-change

Every `provider.state_changed` event begins this rule: a provider moved
between states, so the queue waiting on it may now move, or the moves it
received may now stop. The rule performs two steps and then ends.

1. Call `provider_reroute` with no arguments. Bind the result as `sweep`. The
   outcomes `rerouted` (at least one task moved), `held` (nothing moved, some
   tasks hold with a reason), `idle` (no provider is unavailable, or nothing
   is queued on one) and `disabled` (re-routing is switched off in
   `provider_failover`) all continue to step 2. A `rejected` or
   `runtime_error` outcome fails the rule.
2. Call `provider_availability_notify` with `provider` and `generation` from
   the event. Bind the result as `notice`. It tells the global supervisor and
   the human once per change of availability; the daemon sends the same
   notice when the change happens, so this step normally reports
   `already_notified`. Every outcome other than `rejected` and
   `runtime_error` ends the rule successfully; those two fail it.

## Rule: reroute-sweep

Every `timer.5m` tick begins this rule. A sweep moves at most one pool-width
of work per target and at most `reroute.max_per_sweep` tasks, so the rest of
a dead provider's queue trickles across on these ticks as the moved work is
claimed, rather than landing on the other provider all at once.

1. Call `provider_reroute` with no arguments. Bind the result as `sweep`. The
   outcomes `rerouted`, `held`, `idle` and `disabled` all end the rule
   successfully. A `rejected` or `runtime_error` outcome fails it.

## Failure handling, uniformly

Neither rule retries. A failed step ends the run with a `failed` terminal so
the run overlay shows what broke, and the next five-minute tick is a better
retry than any the run could schedule. A sweep that failed part-way has
moved some tasks and not others; every move it made is recorded on its task
and in `task_reroutes`, and the next sweep simply carries on from the queue
as it finds it.
