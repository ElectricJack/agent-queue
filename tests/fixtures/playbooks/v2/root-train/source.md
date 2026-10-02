---
id: root-train
name: Shared root train
version: 1
scope: system
enabled: false
triggers:
  - integration.sweep_due
  - integration.sealed
  - integration.candidate_green
  - integration.candidate_red
  - integration.repair_delegate_closed
  - integration.repair_exhausted
  - integration.batch_promoted
  - integration.cleanup_requested
---

# Shared root train

This system-scoped policy routes durable root-train facts through the registered
integration services for every project whose frozen integration policy names it as
the root route. A project whose policy does not name it never reaches it. Commands
receive subject identities only; repository, Git objects, leases, fences, policy, CI
evidence, and cleanup targets are resolved from durable server-owned state.

The reviewed graph names `request_id`, `revision`, and `stage`; binds results as
`sealed`, `candidate`, and `rebuilt`; and consumes the already-started bounded
repair stage through `integration_repair_dispatch`.

## Rule: seal-due-frontier

On `integration.sweep_due`, call `integration_seal` with the event `project_id`,
`operation_id` as the durable request identity, and server time. A sealed train
waits for its emitted `integration.sealed` fact. An empty train is released
without constructing a candidate.

## Rule: construct-and-test

On `integration.sealed`, call `integration_build_candidate` with `batch_id` and
bind its typed result. Empty completes. Built and replayed-built candidates call
`integration_ci_evidence` with the same batch and the result revision. Pending
CI ends the current run without repair. Authenticated green or terminal red CI
durably emits an operation-bound continuation. Candidate conflict dispatches
the existing server-derived primary repair stage.
The build adapter applies the frozen `on_main_moved` policy: rebuild is the
default; wait remains a typed non-success outcome. No caller SHA is accepted.

## Rule: promote-green-candidate

On `integration.candidate_green`, call `integration_promote_main` for the exact
current batch and revision.
A moved base rebuilds under the frozen batch policy and waits for the next
durable candidate-result event. If that rebuild conflicts before the repair
deadline, dispatch the exact operation's existing server-derived primary stage.

## Rule: repair-red-candidate

On `integration.candidate_red`, dispatch the exact operation's current repair
stage. The event's `batch_id`, `revision`, and `head_sha` carry routing identity,
while the command resolves durable stage authority server-side.

## Rule: continue-closed-root-repair

On `integration.repair_delegate_closed`, resolve the exact close event and its
`operation_id`, `stage`, `task_id`, `session_id`, `instance_token`, `workspace_id`,
and `fence_token` with `integration_repair_close_current`. Bind the result as
`closed_repair`. Parent delegates end this rule. A stale or
superseded root close fails visibly; it cannot select a newer candidate.
For a current root close, call `integration_build_candidate` with the resolved
batch and `expected_revision` from `closed_repair`, then bind its result as
`repaired_candidate`. Built or already-built candidates call
`integration_ci_evidence` with that result's exact revision. Pending CI ends
the run; authenticated green or red CI emits its own durable continuation.
A build conflict dispatches the exact operation's existing bounded repair stage.
The close event itself never supplies success evidence or promotes a candidate.

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

## Integration decision table

The optional `integration_policy` below is evaluated by the subject reconciler
against `s` (the typed observation) and `subject` (the pinned durable row). It
is reviewed artifact content; `enabled: false` and the legacy event rules remain
in force until the operator approves the shadow evidence and the root cutover.
The table makes one primitive call per visit and maps every closed outcome to
a bounded schedule. An explicit overdue branch covers observations that do not
match a work phase. Binding human holds and unresolved writes precede mutation.
Admission reads authorization data, retains review and source CI requirements,
and accepts `busy` under the current single live batch default. Required checks
and the table are pinned for the lifetime of the subject. Cleanup retains failed
work for seven days. Parent failed-child behavior remains `block`.

Exhaustion defaults to an explicit no-default human gate. A project may review
a different table; copying a template never activates it or authorizes ejection.

```integration-policy
{
  "max_wait_seconds": 3600,
  "tables": {
    "root_batch": {
      "required_checks": {"version": "ci-4c6e0c2a989c", "names": ["Tests (cli-conformance)", "Tests (default-1/8)", "Tests (default-2/8)", "Tests (default-3/8)", "Tests (default-4/8)", "Tests (default-5/8)", "Tests (default-6/8)", "Tests (default-7/8)", "Tests (default-8/8)", "Tests (migration-and-slow)", "Tests (postgres-integration)", "E2E CLI (claims)", "E2E CLI (cli)", "E2E CLI (graphs)", "E2E CLI (failover)"], "producer_id": "15368"},
      "default": "wait",
      "cases": [
        {"rule": "binding-human-hold", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "held"}, "right": {"type": "literal", "value": true}}, "action": "held"},
        {"rule": "unknown-facts", "when": {"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "unknown"}, "mode": "truthy"}, "action": "wait"},
        {"rule": "open-human-gate", "when": {"type": "bool", "op": "and", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "gate"}, "mode": "present"}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "gate.status"}, "right": {"type": "literal", "value": "open"}}]}, "action": "exhaustion-gate"},
        {"rule": "answered-hold", "when": {"type": "bool", "op": "and", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "gate"}, "mode": "present"}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "gate.answer"}, "right": {"type": "literal", "value": "hold"}}]}, "action": "exhaustion-gate"},
        {"rule": "answered-retry", "when": {"type": "bool", "op": "and", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "gate"}, "mode": "present"}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "gate.answer"}, "right": {"type": "literal", "value": "retry"}}]}, "action": "build"},
        {"rule": "reconcile-publication", "when": {"type": "bool", "op": "and", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "unresolved_writes"}, "mode": "truthy"}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "ci_state"}, "right": {"type": "literal", "value": "green"}}, {"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "publisher_fence"}}]}, "action": "publish"},
        {"rule": "unresolved-publication", "when": {"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "unresolved_writes"}, "mode": "truthy"}, "action": "wait"},
        {"rule": "published-cleanup", "when": {"type": "bool", "op": "or", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "published"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "cleaning"}}]}, "action": "cleanup"},
        {"rule": "writer-unclaimed-expired", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "filed"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "budget_expired"}, "right": {"type": "literal", "value": true}}]}, "action": "capacity"},
        {"rule": "writer-live-expired", "when": {"type": "bool", "op": "and", "operands": [{"type": "bool", "op": "or", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "claimed"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "working"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "unknown"}}]}, {"type": "bool", "op": "or", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "budget_expired"}, "right": {"type": "literal", "value": true}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "budget_exhausted"}, "right": {"type": "literal", "value": true}}]}]}, "action": "stop-writer"},
        {"rule": "no-progress-gate", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "no_progress"}, "right": {"type": "literal", "value": true}}, "action": "exhaustion-gate"},
        {"rule": "ladder-exhausted", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "ladder_exhausted"}, "right": {"type": "literal", "value": true}}, "action": "exhaustion-gate"},
        {"rule": "writer-stopped", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "stopped"}}, "action": "build"},
        {"rule": "base-moved", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "base_moved"}, "right": {"type": "literal", "value": true}}, "action": "build"},
        {"rule": "admit-frontier", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "admitting"}}, {"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "members"}, "mode": "truthy"}]}, "action": "seal"},
        {"rule": "admit-empty", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "admitting"}}, "action": "cadence"},
        {"rule": "construct", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "building"}}, "action": "build"},
        {"rule": "primary-repair", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "repairing"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "none"}}]}, "action": "repair-primary"},
        {"rule": "waiting-writer", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "repairing"}}, {"type": "bool", "op": "or", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "filed"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "claimed"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "working"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "unknown"}}]}]}, "action": "wait"},
        {"rule": "promote-exact-green", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "promotable"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "ci_state"}, "right": {"type": "literal", "value": "green"}}, {"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "publisher_fence"}}]}, "action": "publish"},
        {"rule": "publisher-fence-unavailable", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "promotable"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "ci_state"}, "right": {"type": "literal", "value": "green"}}]}, "action": "wait"},
        {"rule": "ci-absent", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "testing"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "ci_state"}, "right": {"type": "literal", "value": "none"}}]}, "action": "request-ci"},
        {"rule": "ci-current", "when": {"type": "bool", "op": "or", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "testing"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "phase"}, "right": {"type": "literal", "value": "promotable"}}]}, "action": "observe-ci"},
        {"rule": "overdue-wait", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "wait_overdue"}, "right": {"type": "literal", "value": true}}, "action": "wait"}
      ],
      "actions": {
        "wait": {
          "primitive": "wait",
          "inputs": {"seconds": {"type": "literal", "value": 60}, "reason": {"type": "literal", "value": "awaiting-facts"}},
          "outcomes": {"unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "waiting": {"kind": "wait", "seconds": 60, "reason": "awaiting-facts"}}
        },
        "cadence": {
          "primitive": "wait",
          "inputs": {"seconds": {"type": "literal", "value": 300}, "reason": {"type": "literal", "value": "admission-cadence"}},
          "outcomes": {"unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "waiting": {"kind": "wait", "seconds": 300, "reason": "admission-cadence"}}
        },
        "held": {
          "primitive": "wait",
          "inputs": {"seconds": {"type": "literal", "value": 60}, "reason": {"type": "literal", "value": "binding-human-hold"}},
          "outcomes": {"unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "waiting": {"kind": "wait", "seconds": 60, "reason": "binding-human-hold"}}
        },
        "seal": {
          "primitive": "integration_seal",
          "inputs": {"admission": {"type": "object", "fields": {"require_review": {"type": "literal", "value": true}, "task_kinds": {"type": "literal", "value": ["feature", "bugfix"]}, "require_source_ci": {"type": "literal", "value": true}, "include_authorized": {"type": "literal", "value": true}}}},
          "outcomes": {"busy": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "empty": {"kind": "close", "reason": "empty-frontier", "phase": "done"}, "sealed": {"kind": "progress", "phase": "building"}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}}
        },
        "build": {
          "primitive": "git_merge_members",
          "inputs": {"target_ref": {"type": "binding_ref", "binding": "subject", "path": "target_ref"}, "base_sha": {"type": "binding_ref", "binding": "s", "path": "default_branch_head"}, "members": {"type": "binding_ref", "binding": "s", "path": "merge_members"}, "regenerate_generated": {"type": "literal", "value": true}},
          "outcomes": {"base_moved": {"kind": "wait", "seconds": 60, "reason": "base-moved", "phase": "building"}, "conflict": {"kind": "progress", "phase": "repairing"}, "merged": {"kind": "progress", "phase": "testing"}, "source_moved": {"kind": "wait", "seconds": 60, "reason": "source-moved", "phase": "building"}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}}
        },
        "request-ci": {
          "primitive": "ci_request",
          "inputs": {"head": {"type": "binding_ref", "binding": "s", "path": "tested_head"}},
          "outcomes": {"already_running": {"kind": "wait", "seconds": 60, "reason": "exact-head-ci", "phase": "testing"}, "requested": {"kind": "wait", "seconds": 60, "reason": "exact-head-ci", "phase": "testing"}, "unavailable": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}}
        },
        "observe-ci": {
          "primitive": "ci_observe",
          "inputs": {"head": {"type": "binding_ref", "binding": "s", "path": "tested_head"}, "required_check_version": {"type": "literal", "value": "ci-4c6e0c2a989c"}},
          "outcomes": {"green": {"kind": "progress", "phase": "promotable"}, "infra": {"kind": "wait", "seconds": 60, "reason": "ci-infrastructure"}, "none": {"kind": "wait", "seconds": 60, "reason": "ci-not-observed"}, "pending": {"kind": "wait", "seconds": 60, "reason": "exact-head-ci"}, "red": {"kind": "progress", "phase": "repairing"}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "untrusted": {"kind": "wait", "seconds": 60, "reason": "untrusted-ci"}}
        },
        "publish": {
          "primitive": "git_publish",
          "inputs": {"fence": {"type": "binding_ref", "binding": "s", "path": "publisher_fence"}, "expected_old_sha": {"type": "binding_ref", "binding": "s", "path": "tested_head.base_sha"}, "new_sha": {"type": "binding_ref", "binding": "s", "path": "tested_head.sha"}, "require_green": {"type": "literal", "value": true}},
          "outcomes": {"published": {"kind": "progress", "phase": "published"}, "target_moved": {"kind": "progress", "phase": "building"}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "unknown_after_push": {"kind": "wait", "seconds": 30, "reason": "ambiguous-publication"}}
        },
        "stop-writer": {
          "primitive": "writer_stop_proof",
          "inputs": {"task_id": {"type": "binding_ref", "binding": "s", "path": "writer.task_id"}, "fence_token": {"type": "binding_ref", "binding": "s", "path": "writer.fence_token"}},
          "outcomes": {"live": {"kind": "wait", "seconds": 30, "reason": "writer-still-live"}, "preserved_and_released": {"kind": "progress", "phase": "repairing"}, "released": {"kind": "progress", "phase": "repairing"}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}}
        },
        "repair-primary": {
          "primitive": "writer_file",
          "inputs": {"role": {"type": "literal", "value": "repair"}, "ordinal": {"type": "literal", "value": 0}, "intelligence_class": {"type": "literal", "value": "standard-high"}, "budget_seconds": {"type": "literal", "value": 1800}, "attempt_limit": {"type": "literal", "value": 3}, "brief": {"type": "literal", "value": "Repair the complete frozen batch; preserve member ancestry and generated sources."}},
          "outcomes": {"configuration_blocked": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "exists": {"kind": "wait", "seconds": 60, "reason": "repair-writer", "phase": "repairing"}, "filed": {"kind": "wait", "seconds": 60, "reason": "repair-writer", "phase": "repairing"}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}}
        },
        "capacity": {
          "primitive": "wait",
          "inputs": {"seconds": {"type": "literal", "value": 1800}, "reason": {"type": "literal", "value": "writer-unclaimed-capacity"}},
          "outcomes": {"unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}, "waiting": {"kind": "wait", "seconds": 1800, "reason": "writer-unclaimed-capacity"}},
          "messages": ["Writer has not been claimed; queue time is not a repair attempt."]
        },
        "exhaustion-gate": {
          "primitive": "gate",
          "inputs": {"question": {"type": "literal", "value": "Repair exhausted on this subject. Retry or keep the human hold?"}, "choices": {"type": "literal", "value": ["retry", "hold"]}, "no_default": {"type": "literal", "value": true}},
          "outcomes": {"answered": {"kind": "progress"}, "created": {"kind": "hold"}, "reused": {"kind": "hold"}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}},
          "answers": {"retry": {"kind": "progress", "phase": "building"}, "hold": {"kind": "hold"}}
        },
        "cleanup": {
          "primitive": "cleanup",
          "inputs": {"delete_successful_sources": {"type": "literal", "value": true}, "retain_failed_seconds": {"type": "literal", "value": 604800}, "max_tries": {"type": "literal", "value": 5}},
          "outcomes": {"clean": {"kind": "close", "phase": "done", "reason": "published-and-clean"}, "irreversible_marker": {"kind": "wait", "seconds": 3600, "reason": "cleanup-marker", "phase": "cleaning"}, "pending": {"kind": "wait", "seconds": 3600, "reason": "cleanup-pending", "phase": "cleaning"}, "unknown": {"kind": "backoff", "seconds": 30, "ceiling_seconds": 3600}}
        }
      }
    }
  }
}
```
