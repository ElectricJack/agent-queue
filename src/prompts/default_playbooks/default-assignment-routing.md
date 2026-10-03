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

Every task a worker runs is routed here, and only here. The orchestrator emits
`task.route_needed` for a queued task that still owes a route and decides
nothing else. This playbook owns the routing rules and the balance between
providers: the routing policy block below is what `task_route_plan` applies,
deterministically, to the task's hints, the live pool capacity, provider
availability and provider usage. An LLM only classifies a task when an answer
the policy needs is missing. It never sees a profile, a provider or a load, so
it cannot choose a model. `task_route_apply` writes the route and refuses every
caller except the project's bound router. A project that wants different rules
runs a reviewed project-scope copy of this file under its own id and is bound to
it; no code changes. The design is
`projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md` §6.

## Rule: route-task

The rule is admitted only when all of these hold:

- the current hydrated `task` row is `DEFINED`, `READY` or `BLOCKED`;
- its `task.route_source` is `unrouted` or `legacy`;
- the event's `router`, the project's bound routing playbook, is
  `default-assignment-routing`, this playbook's id.

The guard reads the current row rather than the event payload: route-needed
events are re-emitted, and one can arrive after another run routed the task or
after the task finished. Such a stale event starts no run. The `router` check
keeps this playbook out of a project bound to another router. An admitted rule
plans the route, classifies the task only when the plan asks for it, and
applies the plan. Each plan has its own binding, because a run binds a name
once.

1. Call `task_route_plan` with `task_id` from the event and `policy`, the text
   of the routing policy block below, verbatim. Bind the result as `plan_a`.
   A `planned` outcome continues to step 5 with `plan_a`: the task's kind and
   class hint decided the route, and no LLM is asked. A
   `needs_classification` outcome continues to step 2. A `held` outcome ends
   the rule: every candidate is on a provider that cannot launch right now,
   and the task waits for one to recover rather than failing a run every two
   minutes for the length of the outage. An `already_routed` outcome ends the
   rule. A `no_candidates`, `rejected` or `runtime_error` outcome fails it.
2. Ask the `playbook-compiler` profile to classify the task. Give it the
   `title`, `description`, `task_type`, `class_hint`, `questions`,
   `allowed_kinds` and `allowed_classes` from `plan_a`, and nothing else.
   Bind the answer as `classification`. The guidance is the "Classifying a
   task" section below. A `completed` outcome continues to step 3. A
   `runtime_error` outcome, where the model failed or answered outside the
   schema, continues to step 4.
3. Call `task_route_plan` with `task_id`, `policy` and `classification`. Bind
   the result as `plan_b`. A `planned` outcome continues to step 5 with
   `plan_b`. A `held` outcome ends the rule, and so does `already_routed`,
   because another run routed the task while this one was classifying it.
   Every other outcome fails the rule.
4. Call `task_route_plan` with `task_id`, `policy` and a `classification` of
   `{"failed": true}`. The classification failed, so the plan proceeds with
   the policy's defaults and treats every lane requirement as unmet: a failed
   classification still routes the task. Bind the result as `plan_c`. A
   `planned` outcome continues to step 5 with `plan_c`. A `held` or
   `already_routed` outcome ends the rule. Every other outcome fails it.
5. Call `task_route_apply` with `task_id` and `plan`, the plan the previous
   step bound: `plan_a`, `plan_b` or `plan_c`. It re-selects among the plan's
   candidates on fresh capacity, so a burst of routes spreads out, writes the
   route with `route_source` `router`, resolves the task's routing gate and
   emits `task.routed`. A `routed` outcome ends the rule. A `stale` outcome
   also ends it and writes nothing: the task was claimed, finished or routed
   after it was planned. A `rejected` or `runtime_error` outcome fails the
   rule.

## Routing policy

This block is passed verbatim, as the string `policy`, to every
`task_route_plan` step. The command validates it and records its digest,
`policy_sha256`, on every route it plans. Profiles are named by class and
harness, never by rung id. Key by key:

- `kinds`: the class a task of that kind gets when it has no hint, and
  `max_class`, the highest class a hint may ask for; a higher hint is clamped.
  A kind with a `lane` goes to that lane. A kind with `narrow: true` may go to
  the OpenCode lanes.
- `origins`: overrides by who filed the task. Integration and development
  repairs are never OpenCode work, and a review dispatch goes to the
  design-review lane.
- `lanes`: `code-design` tries Claude first and falls back to Codex only when
  Claude cannot take the task. `art-design` holds for Codex: `hold: true`
  writes the provider intent `pinned`, so art design waits for its provider
  rather than going elsewhere. `narrow` sends narrow, test-verified work to
  OpenCode while it has a free slot; `narrow-unverified-model` does the same
  only when an independent verifier checks the result. `narrow-hosted` sends
  narrow, test-verified standard-high work to OpenCode on a hosted gateway, the
  `opencode-zen` harness: a harness of its own so that its availability is not
  local OpenCode's, and a lane of its own so that it can be tightened alone.
- `reserved`: keeps deep-high Claude for code design and design review, so a
  hard bug fix hinted deep-high lands on deep-high Codex.
- `balance`: the load score. A candidate's pressure is its live load plus one,
  over its slots times its harness weight, its provider's usage factor and its
  availability factor. The least-pressed candidate wins, and `tie_order`
  breaks a tie, Codex first and hosted OpenCode last.

```yaml
version: 1
class_order: [fast-off, fast-low, fast-high, standard-low, standard-high, deep-low, deep-high]
default_kind: feature
kinds:
  design:   {class: deep-high,     max_class: deep-high,     lane: code-design}
  art:      {class: deep-high,     max_class: deep-high,     lane: art-design}
  research: {class: standard-high, max_class: deep-high}
  feature:  {class: standard-high, max_class: deep-high,     narrow: true}
  bugfix:   {class: standard-high, max_class: deep-high,     narrow: true}
  refactor: {class: standard-high, max_class: deep-high,     narrow: true}
  test:     {class: standard-high, max_class: standard-high, narrow: true}
  docs:     {class: standard-high, max_class: standard-high, narrow: true}
  chore:    {class: fast-high,     max_class: standard-high, narrow: true}
  sync:     {class: fast-high,     max_class: standard-high, narrow: true}
  plan:     {class: deep-high,     max_class: deep-high,     lane: code-design}
origins:
  integration_repair: {narrow: false}
  development_repair: {narrow: false}
  review_dispatch:    {lane: design-review}
lanes:
  code-design:   {class: deep-high, harnesses: [claude, codex], prefer: [claude]}
  art-design:    {class: deep-high, harnesses: [codex], hold: true}
  design-review: {class: deep-high, harnesses: [claude, codex]}
  narrow:
    harnesses: [opencode]
    classes: {standard-high: standard-high, fast-high: fast-low, fast-low: fast-low}
    requires: [narrow, test_verified]
    prefer: true
  narrow-unverified-model:
    harnesses: [opencode]
    classes: {fast-low: fast-off}
    requires: [narrow, test_verified, independent_verifier]
    prefer: true
  narrow-hosted:
    harnesses: [opencode-zen]
    classes: {standard-high: standard-high}
    requires: [narrow, test_verified]
    prefer: true
reserved:
  - {class: deep-high, harness: claude, only_lanes: [code-design, design-review]}
balance:
  harness_weights: {claude: 1.0, codex: 1.0, opencode: 1.0, opencode-zen: 1.0}
  usage_soft_percent: 80
  usage_floor_factor: 0.1
  degraded_factor: 0.5
  tie_order: [codex, claude, opencode, opencode-zen]
```

## Classifying a task

You classify one task for the router. You do not choose a model, a provider or
a profile: the router chooses those from your answer, its policy and live
capacity. Every field of the answer is required. `questions` names the fields
the router actually needs, so take the most care with those.

- `task_type`: one of `allowed_kinds`. Keep the task's own `task_type` when it
  has one. `design` is code design: a spec, an architecture, an implementation
  plan or an API shape. `art` is visual or art-heavy design. `plan` breaks
  approved work into tasks. `research` is an investigation whose deliverable
  is findings rather than a change. `feature`, `bugfix`, `refactor`, `test`,
  `docs`, `chore` and `sync` are what they say.
- `intelligence_class`: one of `allowed_classes`. Keep the `class_hint` when
  the task has one. Default to `standard-high` for ordinary feature
  implementation, debugging, refactoring, tests, and coordinated changes
  across modules; most development tasks belong there. A task described as
  straightforward, routine, conventional, or an ordinary dependency or bug fix
  is `standard-high`. Use a fast class only for clearly trivial, localized
  work whose requirements are already settled. Use a deep class only for
  design, or for exceptionally difficult work: a genuinely unresolved
  architectural problem, a hard investigation with concrete evidence that
  standard reasoning was insufficient, or unusually complex correctness
  reasoning. An unfamiliar library or language, several files, native builds,
  integration tests, high priority, and a failing CI run alone are not
  reasons for a deep class.
- `narrow`: true when the work is well specified with a narrow footprint: the
  requirements are settled and the files to change are named or obvious.
- `test_verified`: true when an existing or specified test checks the result.
  End-to-end or manual-verification work is not test-verified.
- `independent_verifier`: true only when the task names a verifier that
  someone else runs, such as a test or check outside the change itself.
- `reason`: one or two sentences, at most 400 characters, naming the evidence
  for the answer.

Return exactly one JSON object with this shape and no extra fields:

```json
{"task_type":"<one of allowed_kinds>","intelligence_class":"<one of allowed_classes>","narrow":false,"test_verified":false,"independent_verifier":false,"reason":"<the evidence>"}
```

## Failure handling, uniformly

The rule has no retry. A failed step ends the run with a `failed` terminal so
the run overlay shows what broke, and `aq task explain` names the run. The task
stays unrouted and visible, and no worker can claim it. The orchestrator
re-emits `task.route_needed` for a task that is still unrouted, at most every
two minutes, so a transient failure is retried by the next event and a
permanent one (no worker candidate satisfies the policy) stays visible until
an operator adds a profile or binds the project to another router. A failed
classification is not a failed run: the plan proceeds with the policy's
defaults.
