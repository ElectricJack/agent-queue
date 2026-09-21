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

The rule is admitted only when the current hydrated task row is `DEFINED`,
`READY`, or `BLOCKED` and still lacks either its intelligence class or worker
profile. This is deliberately a current-row guard rather than a condition on
the event payload: route-needed events are retried and can arrive after a
successful route or task completion. Such stale events must start no run and
must never reach the chooser. An admitted rule reads the task's routing state
and takes exactly one of three paths.

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

Default to `standard-high` for ordinary feature implementation, debugging,
refactoring, tests, and coordinated changes across modules. Most development
tasks belong in this class. Use a fast class only for clearly trivial, localized
work whose requirements are already settled.

When the task is described as straightforward, routine, conventional, or an
ordinary dependency or bug fix, select `standard-high` whenever it is offered.
Those descriptions are affirmative evidence for the default, not a reason to
escalate. Do not select a deep class merely because the task involves an
unfamiliar library or language, several files, or a failing test.

The `deep-high` row on the `anthropic` provider (`deep-high-claude`) is for code
design only: a spec, an architecture, an implementation plan, or an API shape.
Never choose it for a bug fix, a repair, an implementation, tests, or
documentation, however hard that work looks; route those to another row.

Outside design, use a deep class or `astra-high` only for exceptionally
difficult work: a genuinely unresolved architectural problem, a hard
investigation with concrete evidence that standard reasoning was insufficient,
or unusually complex correctness reasoning. The reason must name that specific
difficulty and explain why `standard-high` is insufficient. C++, multiple files,
native builds, integration tests, high priority, and a failing CI run alone are
not reasons to select one of them. `astra-*` exists only on OpenAI, so it is
selectable only when a supplied row offers it.

OpenCode rows are the ones whose `profile_id` ends in `-opencode`. Prefer one
for well-specified, test-verified implementation with a narrow footprint: the
requirements are settled, the files to change are named or obvious, and an
existing or specified test checks the result. Choose `standard-high-opencode`
for that work and `fast-low-opencode` for trivial mechanical edits, whenever the
row is offered. Choose `fast-off-opencode` only when the task names an
independent verifier, such as a test or check someone else runs, because that
model can report success on a file it has broken. Integration repairs (titled
`Repair development integration: ...`) and end-to-end or manual-verification
work are never OpenCode work.

Choose a class and compatible profile only from the supplied options. Preserve
explicit operator assignments. If `standard-high` is absent, select the closest
suitable available standard class and explain the fallback; do not promote to
deep just because workers are temporarily occupied.

Copy `provider` from the chosen row. Temporary worker occupancy is not a reason
to change the required intelligence class. When several rows offer the chosen
class, apply the OpenCode guidance above first; otherwise prefer a `pool`
lifecycle row, then the row with idle capacity.

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
