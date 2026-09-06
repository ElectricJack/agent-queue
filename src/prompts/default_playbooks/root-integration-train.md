---
id: root-integration-train
name: Root integration train
version: 1
scope: system
enabled: false
triggers:
  - integration.sweep_due
  - integration.sealed
  - integration.repair_exhausted
  - integration.batch_promoted
  - integration.cleanup_requested
---

# Root integration train

This disabled policy routes durable root-train facts through the registered
integration services. Commands receive subject identities only; repository,
Git objects, leases, fences, policy, CI evidence, and cleanup targets are
resolved from durable server-owned state.

The reviewed graph names `request_id`, `revision`, `stage`, `starting_sha`, and
`trigger_id`; binds results as `sealed`, `candidate`, and `rebuilt`; and uses
the existing `integration_repair_start` and `integration_repair_dispatch`
commands for the bounded repair route.

## Rule: seal-due-frontier

On `integration.sweep_due`, call `integration_seal` with the event `project_id`,
`operation_id` as the durable request identity, and server time. A sealed train
waits for its emitted `integration.sealed` fact. An empty train is released
without constructing a candidate.

## Rule: construct-and-test

On `integration.sealed`, call `integration_build_candidate` with `batch_id` and
bind its typed result. Empty completes. Built and replayed-built candidates call
`integration_ci_evidence` with the same batch and the result revision. Green
calls `integration_promote_main` for that exact batch and revision. Candidate
conflict or red CI starts and dispatches the existing bounded primary repair.
The build adapter applies the frozen `on_main_moved` policy: rebuild is the
default; wait remains a typed non-success outcome. No caller SHA is accepted.

## Rule: dispatch-debug

On `integration.repair_exhausted`, dispatch the exact operation's existing
debug stage. Exhausted debug or human-required dispatch ends visibly failed;
this policy never creates an unbounded replacement budget.

## Rule: release-promoted

On `integration.batch_promoted`, call `integration_release` with `batch_id`.
Release is independent of cleanup progress and consumes only terminal exact
main-delivery evidence.

## Rule: cleanup-promoted

On `integration.cleanup_requested`, call `integration_cleanup` with `batch_id`.
The server materializes and advances normalized cleanup. The playbook supplies
no ref, SHA, PR, repository, owner, lease, or execution nonce.

## Failure handling

Typed stale, wait, configuration, conflict, exhausted, and human outcomes end
the current run without fabricating success. Durable events and the bounded
integration service drive safe replay.
