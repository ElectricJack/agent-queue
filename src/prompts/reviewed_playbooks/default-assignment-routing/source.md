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

Every `task.route_needed` event begins the one `route-task` rule. The
orchestrator emits that event for a task that lacks the fields a worker needs
to pick it up — an `intelligence_class`, a `profile_id`, or both — and decides
nothing else. This playbook is the routing policy: it chooses the class the
task needs and the profile that serves it, and writes both onto the task. A
project that wants different routing keeps a project-scope copy of this file;
no code changes. The design is
`docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md`.

## Rule: route-task

There is no guard. The rule reads the task's routing state and takes exactly
one of three paths.

1. Call `task_route_options` with `task_id` from the event. Bind the result as
   `routing`. It reports the task's fields, whether its class is explicit, and
   the `options` catalog — one row per intelligence class, provider and
   profile an ordinary worker can execute, with configured, idle and busy
   counts. An `already_routed` outcome ends the rule: the task carries a
   class and a profile that serves it. An `explicit` outcome — the operator
   already fixed the class, and `routing.explicit_profile_id` names the
   profile that serves it — continues to step 3. An `undecided` outcome
   continues to step 2. A `no_options`, `rejected`, or `runtime_error`
   outcome fails the rule.
2. Ask the `playbook-compiler` profile to choose the route. Give it the task's
   `title`, `description`, `priority` and `task_type`, and the `options`
   rows, all from `routing`. Bind the answer as `decision`: an
   `intelligence_class`, a `provider`, a `profile_id`, and a `reason`. The
   guidance is the "Choosing a class" section below. A `completed` outcome
   continues to step 4; a `runtime_error` outcome fails the rule.
3. Call `task_route` with `task_id` from the event, `profile_id`
   `routing.explicit_profile_id`, `intelligence_class`
   `routing.intelligence_class`, and `reason` `explicit intelligence class`.
   A `routed` outcome ends the rule; a `rejected` or `runtime_error` outcome
   fails it.
4. Call `task_route` with `task_id` from the event, `profile_id`
   `decision.profile_id`, `intelligence_class` `decision.intelligence_class`,
   and `reason` `decision.reason`. It writes the class and the profile onto
   the task under the "no worker holds it" predicate and resolves the task's
   routing gate. A `routed` outcome ends the rule; a `rejected` or
   `runtime_error` outcome fails it.

## Choosing a class

Choose the least expensive intelligence class that can reliably complete the
task. Use the title, description, task type and priority, and the supplied
options. The options are binding: choose `intelligence_class`, `provider` and
`profile_id` together from one options row, and never name a class, provider
or profile that is absent from the rows.

Prefer a fast, low-reasoning class for routine, localized, well-specified work.
Use a standard class when the task needs several coordinated edits, debugging,
or judgment across modules. Reserve deep classes for ambiguous architecture,
high-risk changes, hard investigations, and work whose failure would be costly.

Copy `provider` from the chosen row. Temporary worker occupancy is not a reason
to change the required intelligence class; when two rows offer the same class,
prefer a `pool` lifecycle row, then the row with idle capacity.

Give a concise, non-empty `reason`. Return exactly one JSON object with this
shape and no extra fields:

```json
{"intelligence_class":"<class from a row>","provider":"<provider from that row>","profile_id":"<profile from that row>","reason":"<why this class fits>"}
```

## Failure handling, uniformly

The rule has no retry. A failed step ends the run with a `failed` terminal so
the run overlay shows what broke, and `aq task explain` names the run. The
orchestrator re-emits `task.route_needed` for a task that is still unrouted,
at most every two minutes, so a transient failure is retried by the next
event and a permanent one — no profile can execute the class — stays visible
until an operator adds a profile or pins the task by hand.
