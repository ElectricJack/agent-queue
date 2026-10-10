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
the policy needs is missing. Because the policy's `risk` table can raise a
class or narrow the harnesses, the risk answer is needed for almost every task,
so almost every task is classified. The classifier never sees a profile, a
provider, a load or the task's priority, so it cannot choose a model.
`task_route_apply` writes the route and refuses every
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
   route with `route_source` `router` and fresh fit/capacity/quota evidence
   (including snapshot and quota age), resolves the task's routing gate and
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
- `risk`: what the classifier's `risk` answer does. The classifier judges
  how much damage the change could do if it is wrong, separately from how hard
  it is; the "Classifying a task" section below gives the guidance. A level
  with a rule gets that rule's `min_class` as a floor: a lower class, whether
  it came from the kind, a hint or the classifier, is raised to the floor, and
  the plan records `class_raised_for_risk`. The floor beats a kind's
  `max_class` and a class hint. Only an operator route override goes under it.
  `harnesses` limits every candidate to those harnesses: medium, high and very
  high risk work runs only on Claude or Codex, never on local OpenCode, the
  free hosted models or any other harness. `relax` lowers a level's floor when
  the classifier's answer meets every flag it `requires`: very-high-risk work
  leans to a deep class but may stay at standard-high when it is narrow and
  test-verified. The policy never forces a deep class on medium- or high-risk
  work; the classifier picks one when risky work is also complex (see
  "Classifying a task"). Low risk has no rule, so low-risk
  work routes exactly as the rest of this policy says. A failed or missing risk
  answer is treated as `medium`, because not knowing is not evidence the change
  is safe: it gets the medium floor and harnesses, the plan records the class
  it raised with `assumed: true`, and every lane with `max_risk: low` stays
  closed.
- `origins`: overrides by who filed the task. Integration and development
  repairs are never OpenCode work, and a review dispatch goes to the
  design-review lane.
- `lanes`: `code-design` tries Claude first and falls back to Codex only when
  Claude cannot take the task. `art-design` holds for Codex: `hold: true`
  writes the provider intent `pinned`, so art design waits for its provider
  rather than going elsewhere. The three narrow lanes are the cheap lanes, and
  each takes only work the classifier judged low risk (`max_risk: low`).
  Narrow and test-verified alone no longer qualifies work for them, and
  neither does a precisely written description. `narrow` sends narrow,
  test-verified, low-risk work to local OpenCode while it has a free slot;
  `narrow-unverified-model` does the same only when an independent verifier
  checks the result. `narrow-hosted` sends trivial work to OpenCode on a hosted
  gateway: the `opencode-zen` harness (Space Bunny Free),
  `opencode-zen-nemotron` (Nemotron 3 Ultra free) and `opencode-zen-longcat`
  (LongCat 2.5 Preview free). Until a hosted model has a track record (see
  "Earning a wider lane"), the lane takes only work the classifier put in a
  fast class (trivial, localized, settled): narrow, test-verified, low-risk
  chores, run on the model's one `standard-high` rung. The lane matches
  `opencode-zen*`, so new Zen preview harnesses cannot become general
  candidates or take integration/development repairs. Each
  hosted model is its own harness, so each has an independent
  availability row (free-tier exhaustion is separate from local OpenCode's and
  from the other hosted models), and they share one lane so the lane can be
  tightened as a unit.
- Priority is not a routing input. No lane and no local-model rule has a
  priority floor or ceiling, so a task's priority never chooses its class,
  harness, lane or model; it only orders the claim frontier. A cheap lane
  admits work by its class, its `requires` flags and its `max_risk` alone.
  The `local_models` gate keeps its defaults: an integration-train bug fix,
  and work other tasks wait on, never run on a self-hosted model.
- `reserved`: keeps deep-high Claude for code design and design review, so a
  hard bug fix hinted deep-high lands on deep-high Codex.
- `balance`: the load score. A candidate's pressure is its live load plus one,
  over its slots times its harness weight, its provider's usage factor and its
  availability factor. The least-pressed candidate wins, and `tie_order`
  breaks a tie, Codex first and local OpenCode before each hosted OpenCode
  lane, Space Bunny first. `prefer_harnesses` on ordinary implementation
  kinds favors compatible Codex while available, within its own usage soft
  limit and with positive effective headroom. Eligible OpenCode lanes are
  considered first. Saturated, unavailable, degraded or quota-pressured Codex
  falls back to compatible free hosted capacity, then queued pressure. Unknown
  usage stays unknown; stale/reset quota is evidence only. No percentages are
  compared across providers or unlike windows, and load never raises a class.

```yaml
version: 1
class_order: [fast-off, fast-low, fast-high, standard-low, standard-high, deep-low, deep-high]
default_kind: feature
kinds:
  design:   {class: deep-high,     max_class: deep-high,     lane: code-design}
  art:      {class: deep-high,     max_class: deep-high,     lane: art-design}
  research: {class: standard-high, max_class: deep-high}
  feature:  {class: standard-high, max_class: deep-high,     narrow: true, prefer_harnesses: [codex]}
  bugfix:   {class: standard-high, max_class: deep-high,     narrow: true, prefer_harnesses: [codex]}
  refactor: {class: standard-high, max_class: deep-high,     narrow: true, prefer_harnesses: [codex]}
  test:     {class: standard-high, max_class: standard-high, narrow: true, prefer_harnesses: [codex]}
  docs:     {class: standard-high, max_class: standard-high, narrow: true, prefer_harnesses: [codex]}
  chore:    {class: fast-high,     max_class: standard-high, narrow: true, prefer_harnesses: [codex]}
  sync:     {class: fast-high,     max_class: standard-high, narrow: true, prefer_harnesses: [codex]}
  plan:     {class: deep-high,     max_class: deep-high,     lane: code-design}
origins:
  integration_repair: {narrow: false}
  development_repair: {narrow: false}
  review_dispatch:    {lane: design-review}
risk:
  medium:    {min_class: standard-high, harnesses: [claude, codex]}
  high:      {min_class: standard-high, harnesses: [claude, codex]}
  very_high:
    min_class: deep-low
    harnesses: [claude, codex]
    relax: {class: standard-high, requires: [narrow, test_verified]}
lanes:
  code-design:   {class: deep-high, harnesses: [claude, codex], prefer: [claude]}
  art-design:    {class: deep-high, harnesses: [codex], hold: true}
  design-review: {class: deep-high, harnesses: [claude, codex]}
  narrow:
    harnesses: [opencode]
    classes: {standard-high: standard-high, fast-high: fast-low, fast-low: fast-low}
    requires: [narrow, test_verified]
    max_risk: low
    prefer: true
  narrow-unverified-model:
    harnesses: [opencode]
    classes: {fast-low: fast-off}
    requires: [narrow, test_verified, independent_verifier]
    max_risk: low
    prefer: true
  narrow-hosted:
    harnesses: [opencode-zen*]
    classes: {fast-high: standard-high, fast-low: standard-high}
    requires: [narrow, test_verified]
    max_risk: low
    prefer: true
reserved:
  - {class: deep-high, harness: claude, only_lanes: [code-design, design-review]}
balance:
  harness_weights: {claude: 1.0, codex: 1.0, opencode: 1.0, opencode-zen: 1.0, opencode-zen-nemotron: 1.0, opencode-zen-longcat: 1.0}
  usage_soft_percent: 80
  usage_floor_factor: 0.1
  degraded_factor: 0.5
  tie_order: [codex, claude, opencode, opencode-zen, opencode-zen-nemotron, opencode-zen-longcat]
```

## Earning a wider lane

The free hosted models start on probation: the `narrow-hosted` lane above
takes only trivial (fast-class), narrow, test-verified, low-risk work.
Nothing in the router measures a track record, and nothing widens a lane by
itself. A record is evidence that an operator reads before approving a change
to this playbook, the same reviewed flow as this revision.

- **Per harness.** Each hosted model is its own harness, so each earns its own
  record. One model's record never widens another's lane.
- **A clean pass** is a task this policy routed to that harness that closed
  `pass` and whose delivery reached the default branch, and that within 14 days
  of landing was not reopened, was not reverted, and had no integration or
  development repair, and no bug fix, filed against it.
- **Earning stage 2.** 10 consecutive clean passes. Every task routed to the
  harness counts, including a failed close, which ends the run of passes. A
  route an operator overrode does not count either way. Stage 2 is a reviewed
  change that moves the proven harness into its own lane (for example
  `narrow-hosted-proven`). That lane adds `standard-high: standard-high`, so
  ordinary narrow, test-verified, low-risk work qualifies. The `opencode-zen*`
  lane keeps every unproven and newly installed Zen model on probation, and
  still keeps them out of the general candidates.
- **No stage 3.** Medium, high and very high risk work never goes to a free
  hosted or local model, however long its record. The `risk` table enforces
  this, not the lane.
- **Losing the record.** Two tasks with rework among a stage-2 harness's
  last 10 routed tasks return it to probation by the same reviewed change.
- **Reading the record.** A task's route evidence names its profile and
  harness. Its close outcome, its repair children and its landing are task
  history. `aq task explain` and the route columns answer it today, and a
  supervisor report can count it. No new mechanism is needed to read a record.

## Classifying a task

You classify one task for the router. You do not choose a model, a provider or
a profile: the router chooses those from your answer, its policy and live
capacity. Ordinary hosted implementation, tests and fixes prefer Codex
when compatible capacity permits, to conserve Claude budget; eligible narrow
local work still takes its verified lane first. This preference is applied by
the policy, not by your JSON answer. Classify the actual deliverable and keep
operator hints, design/art requirements and verification requirements intact.
Provider busyness is never evidence for a higher intelligence class, and you
are not shown the task's priority. Every field of the answer is required.
`questions` names the fields the router actually needs, so take the most care
with those.

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
  work whose requirements are already settled. Use a deep class for design;
  for very-high-risk work unless the change is narrow and test-verified; for
  high- or very-high-risk work that is also complex, such as a redesign of how
  the system recovers, delivers or migrates; and for exceptionally difficult
  work: a genuinely unresolved architectural problem, a hard investigation
  with concrete evidence that standard reasoning was insufficient, or unusually
  complex correctness reasoning. Not all high-risk work is deep work: a
  high-risk change that is ordinary to make stays `standard-high`, and the
  router raises the class itself where the policy says so. An unfamiliar
  library or language, several files, native builds, integration tests and a
  failing CI run alone are not reasons for a deep class.
- `risk`: how much damage the change could do if it is wrong, judged
  separately from how hard it is. One of `low`, `medium`, `high` or
  `very_high`. These are examples, not rules; judge the actual change.
  - `very_high` or `high`: the delivery and integration pipeline (landing,
    trains, merges, CI gating); crash, owner or session recovery; git branch
    handling (creating, moving, rebasing or deleting branches or refs);
    database migrations and changes to stored data; authentication,
    permissions and secrets; money and the live trader (`quilt-trader`); and
    deleting data or branches. Prefer `very_high` when a mistake would be
    hard to undo or would reach outside the repository: deleting branches on
    origin, migrating or deleting stored data, or anything that can place or
    size a live trade.
  - `medium`: ordinary feature work, fixes and refactors in shared code that
    other work depends on.
  - `low`: documentation, UI copy, isolated components and tests, where a
    mistake is visible and cheap to undo.
  A narrow, precisely written task is not lower risk for being precise: risk
  is about what the change touches, never about how well the task is
  described. When the task touches more than one area, answer for the
  riskiest. When you cannot tell, answer `medium`.
- `risk_reason`: one sentence, at most 400 characters, naming what the change
  touches that sets its risk.
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
{"task_type":"<one of allowed_kinds>","intelligence_class":"<one of allowed_classes>","narrow":false,"test_verified":false,"independent_verifier":false,"reason":"<the evidence>","risk":"<low|medium|high|very_high>","risk_reason":"<what sets the risk>"}
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
defaults, and with no risk answer it treats the risk as `medium`: the medium
floor and harnesses apply, and every cheap lane stays closed.
