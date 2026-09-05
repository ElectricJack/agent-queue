---
id: ci-main-sentinel
kind: pipeline
role: ci-main-sentinel
scope: project:agent-queue
enabled: true
triggers:
  - timer.15m
cooldown: 600
---

# CI main sentinel

Every `timer.15m` event for `project:agent-queue` begins the one
`keep-main-green` rule. The `cooldown: 600` field is documentary metadata: the
timer interval is longer than 600 seconds, so it does not suppress any event.

The sentinel owns the health of the default branch. It never edits code
itself: it observes CI, files one repair task per distinct failure, and hands
a failure that two repairs could not fix to a human. The design is
`docs/superpowers/specs/2026-09-05-ci-main-sentinel-design.md`.

## Rule: keep-main-green

There is no guard. The rule reads the default branch's CI verdict and then
takes exactly one of three paths.

1. Call `ci_baseline_status` with `project_id` `agent-queue`. Bind the result
   as `baseline`. It judges the head commit's check runs, names the failing
   checks and tests, and keys the repair by their failure signature. A `green`,
   `pending`, or `unknown` outcome ends the rule: there is nothing to repair,
   or nothing to repair yet, and the next tick looks again. A `red` outcome
   continues to step 2. A `red_escalated` outcome — the same signature has
   already spent its repair attempts — continues to step 3. A `rejected` or
   `runtime_error` outcome fails the rule.
2. Call `ensure_task` with `project_id` `agent-queue`, `dedup_key`
   `baseline.dedup_key`, `title` `baseline.title`, `description`
   `baseline.description`, `priority` `5`, and `intelligence_class`
   `deep-high`. Bind the resulting task as `repair`. The key is
   `ci-baseline:<signature>:<attempt>`, so a commit that leaves the same tests
   red reuses the in-flight repair and a different failure gets its own task.
   A `created` or `reused` outcome ends the rule; a `rejected` or
   `runtime_error` outcome fails it.
3. Call `gate_create` with `project_id` `agent-queue`, `gate_type` `human`,
   `title` `baseline.escalation_title`, `question`
   `baseline.escalation_question`, and `await_id` `baseline.escalation_key`.
   The key is `ci-baseline-escalation:<signature>`, so the gate opens once per
   failure. A `created`, `reused`, or `skipped` outcome ends the rule; a
   `rejected` or `runtime_error` outcome fails it.

## Failure handling, uniformly

The rule has no retry. A failed step ends the run with a `failed` terminal so
the run overlay shows what broke; the next timer tick starts over from the
observation. Deduplication is the safety guard against both a repair storm and
a gate storm. The project scope is the authorization boundary: every command
runs only for `agent-queue`.
