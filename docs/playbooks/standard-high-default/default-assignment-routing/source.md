---
id: default-assignment-routing
kind: pipeline
role: assignment-routing
profile_id: playbook-compiler
scope: system
enabled: true
triggers:
  - task.route_needed
max_tokens: 4096
llm_config:
  intelligence_class: fast-low
---

# Default assignment routing

Every `task.route_needed` event begins the one `route-task` rule. This
playbook chooses difficulty and a compatible profile from the supplied options.
Explicit profile pins are preserved. A project may override difficulty policy
with a project-scope copy of this file.

## Rule: route-task

There is no guard. The rule reads the task's triage state.

1. Call `task_route_options` with `task_id` from the event. Bind the result as
   `routing`. The `options` catalog lists executable classes. An
   `already_routed` outcome ends the rule. An `explicit` outcome continues
   to step 3. An `undecided` outcome continues to step 2. A `no_options`,
   `rejected`, or `runtime_error` outcome fails the rule.
2. Ask the `playbook-compiler` profile to choose the difficulty class and a compatible profile.
   Give it `title`, `description`, `priority`, `task_type`, and `options`
   from `routing`. Bind the answer as `decision`: `intelligence_class`, `profile_id`, and
   `reason`. Use the "Choosing a class" guidance below. A `completed`
   outcome continues to step 4; a `runtime_error` outcome fails the rule.
3. Call `task_route` with `task_id` from the event, `intelligence_class`
   `routing.intelligence_class`, and `reason` `explicit intelligence class`.
   Pass `profile_id` from `routing.explicit_profile_id`. A `routed` outcome ends the rule; `rejected` or
   `runtime_error` fails it.
4. Call `task_route` with `task_id` from the event, `intelligence_class`
   `decision.intelligence_class`, `profile_id` `decision.profile_id`, and `reason` `decision.reason`.
   The command preserves an existing explicit profile pin and
   resolves the routing gate. A `routed` outcome ends the rule; `rejected`
   or `runtime_error` fails it.

## Choosing a class

Default to `standard-high` for ordinary feature implementation, debugging,
refactoring, tests, and coordinated changes across modules. Most development
tasks belong in this class. Use a fast class only for clearly trivial, localized
work whose requirements are already settled.

Use `deep-high` only for exceptionally difficult work: a genuinely unresolved
architectural problem, a hard investigation with concrete evidence that standard
reasoning was insufficient, or unusually complex correctness reasoning. The
reason must name that specific difficulty and explain why `standard-high` is
insufficient. C++, multiple files, native builds, integration tests, high priority,
and a failing CI run alone are not reasons to select `deep-high`.

Choose a class and compatible profile only from the supplied options. Preserve
explicit operator assignments. If `standard-high` is absent, select the closest
suitable available standard class and explain the fallback; do not promote to
deep just because workers are temporarily occupied.

Give a concise, non-empty reason describing difficulty. Return exactly:

```json
{"intelligence_class":"<class from options>","profile_id":"<profile from the same options row>","reason":"<why this difficulty fits>"}
```

## Failure handling, uniformly

The rule has no retry. A failed step ends the run with a `failed` terminal so
the run overlay shows what broke, and `aq task explain` names the run. The
orchestrator re-emits `task.route_needed` for a task that is still unrouted,
at most every two minutes, so a transient failure is retried by the next
event and a permanent one — no profile can execute the class — stays visible
until an operator adds a profile or pins the task by hand.