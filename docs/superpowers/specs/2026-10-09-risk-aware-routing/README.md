# Risk-aware routing: draft revision of `default-assignment-routing`

**Status:** approved (`rev-azure-flare`); drafted in task `smart-beacon-29`.
Steps 1 and 2 of "Activating after approval" landed in `solid-apex-59`, with
Jack's answers to the open questions applied (see "Decisions"). Where this
document and its draft (`default-assignment-routing.md`, `bundle/`) say
priority is a ceiling, or that a missing risk answer applies no floor, the
decisions below and the shipped source supersede them.

## What changes

The router gains a **risk** judgement next to difficulty. The classifier
answers `risk` (`low`, `medium`, `high` or `very_high`) and a one-sentence
`risk_reason`. A new `risk` table in the policy block turns that answer into
a class floor and a harness limit:

| Risk | Complexity | Route in the draft |
|---|---|---|
| low | any | Today's rules. The cheap lanes (local OpenCode, free hosted) admit it only when narrow, test-verified, low risk and below their priority ceiling. |
| medium, high | low to normal | Floor `standard-high`, harnesses `claude` and `codex` only. Never local OpenCode or the free hosted models. |
| very high | normal | Floor `deep-low` on Claude or Codex. Relaxed to `standard-high` when the classifier says narrow **and** test-verified. |
| very high, or high + complex | complex | Deep. The classifier picks a deep class for risky work that is also complex; the guidance no longer reserves deep for design and exceptionally difficult work. |

How each of the five agreed points is met:

1. **Risk next to difficulty.** The classify step's output schema requires
   `risk` (an enum) and `risk_reason`. The classifier guidance gives the
   agreed examples as examples, not rules, says to answer for the riskiest
   area touched and to answer `medium` when unsure.
2. **Risk pushes work up.** The `risk` table: `medium` and `high` floor
   `standard-high` on `[claude, codex]`; `very_high` floors `deep-low` on
   `[claude, codex]` with `relax: {class: standard-high, requires: [narrow,
   test_verified]}`. The floor beats a kind's `max_class` and a filer's class
   hint; only an operator route override goes under it. A raised plan records
   `class_raised_for_risk`.
3. **Narrow + test-verified is not enough.** All three cheap lanes carry
   `max_risk: low`, so they need a low-risk answer as well. The guidance says
   a precisely written description does not lower risk, and the classifier is
   never shown priority or capacity.
4. **Priority is only a ceiling on the cheap lanes.** `local_models:
   below_priority: 150` (the existing default, now written out) and
   `below_priority: 101` on the free hosted lane. Priority never chooses a
   class, harness or lane in any other way.
5. **Free hosted models on probation.** The `narrow-hosted` lane maps only
   the fast classes (`fast-high`, `fast-low`) onto the hosted model's one
   `standard-high` rung, so it takes trivial, low-risk chores at default
   priority or below. Today it takes any narrow, tested `standard-high`
   work. "Earning a wider lane" defines the
   record: per harness, 10 consecutive clean passes (closed `pass`, landed on
   the default branch, no reopen, revert, repair or bug fix within 14 days),
   then a reviewed change moves that harness into its own lane with
   `standard-high` work and the local ceiling. There is no stage 3: medium and
   higher risk never reach a free or local model.

## Files

| File | What |
|---|---|
| [`default-assignment-routing.md`](default-assignment-routing.md) | The revised playbook source, drafted against the shipped reviewed copy (`src/prompts/reviewed_playbooks/default-assignment-routing/source.md`). |
| [`bundle/`](bundle/) | The draft compiled by the reviewed-bundle compiler: `artifact.json`, `diagnostics.json` (empty), `manifest.md`. |
| [`dry-run.py`](dry-run.py), [`dry-run.md`](dry-run.md) | The worked examples run through `plan_route` under the installed policy (the approval-time run compared the pre-revision policy with the draft). |
| `src/routing/policy.py`, `src/routing/planner.py` | The mechanism the draft needs (below). Inert: a policy without the new keys plans exactly as before. |
| `tests/test_routing_planner.py`, `tests/test_routing_router.py` | 14 tests for the mechanism, including the shipped policy's unchanged digest. |
| `src/commands/routing_commands.py` | `task_route_apply` copies `class_raised_for_risk` into the route record. |
| `scripts/rebuild-reviewed-playbook-artifacts.py` | Builds the classify step's output schema from the policy, and compiles a draft with `--source`/`--out`. |

## Router output changes

- **Classifier answer** (the classify step's `output_schema`): adds required
  `risk` (`enum: [low, medium, high, very_high]`) and `risk_reason` (1–400
  characters) when, and only when, the policy block has a `risk` table. The
  shipped bundle, which has none, compiles byte-identically.
- **Plan** (`task_route_plan`'s `planned` value): `classification` gains
  `risk` and `risk_reason` when the answer carried them, and a new
  `class_raised_for_risk: {from, to, risk}` appears only when a floor raised
  the class. `questions` may now include `risk` and the flags a `relax` rule
  needs.
- **Policy schema** (all optional): top-level `risk: {<level>: {min_class,
  harnesses, relax: {class, requires}}}`; narrow-lane `max_risk` and
  `below_priority`.

Two consequences to weigh:

- **More classifier calls.** The plan asks `risk` whenever the answer could
  raise the class or remove a candidate's harness. Typed tasks with
  `narrow: true` were already classified for the narrow lanes; a typed
  `research` task, which today plans without the LLM, now gets one call.
- **The playbook path drops `class_raised_for_risk` before
  `task_route_apply`.** The typed contract `TaskRoutePlanValue`
  (`src/commands/contracts/builtin.py`) keeps only the fields it declares.
  The route still runs at the raised class, because the plan's
  `intelligence_class` is already raised; only the record of the raise is
  lost. Adding the field changes the contract's fingerprint and so stales
  every reviewed bundle that uses it, so it belongs in the activation change
  (step 1 below), when the bundles are rebuilt anyway.

Mechanism in code, policy in the playbook: the planner applies the table
deterministically, and every threshold above lives in the reviewed markdown.

## Worked examples

[`dry-run.py`](dry-run.py) runs each example through `plan_route` on a model
fleet: one rung per class on `claude` and `codex`, three local OpenCode rungs
and the three free hosted Zen rungs at `standard-high`, all idle unless the
row says the local slots are busy. The risk and flags are the classifier
answers I would expect; the seven fleet-ridge-45 rows use the real subtasks'
kind (`feature`, `chore` for 45.7), class hint and priority 230. *Before* is
the pre-revision policy as recorded at approval; *Installed* is the policy as
shipped after Jack's decisions (*Decisions* below), recorded in
[`dry-run.md`](dry-run.md). The approved draft routed the two rows marked †
to `standard-high-codex` and `fast-high-codex`: its priority ceilings kept
them off the cheap lanes. Every row asks the classifier `narrow`, `risk` and
`test_verified`.

| Example | Prio | Risk | Before | Installed |
|---|---|---|---|---|
| fleet-ridge-45.1 recovery resumes on the task's own branch | 230 | very_high | `standard-high-codex` | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.2 delete branches on land | 230 | very_high | `standard-high-opencode-zen` (lane narrow-hosted) | `standard-high-codex` |
| fleet-ridge-45.3 delete branches on abandon + audit table | 230 | very_high | `standard-high-codex` | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.4 hand-landing merges, never rebases | 230 | high | `standard-high-opencode-zen` (lane narrow-hosted) | `standard-high-codex` |
| fleet-ridge-45.4 docs-only slice (guide text) | 100 | low | `standard-high-opencode` (lane narrow) | `standard-high-opencode` (lane narrow) |
| fleet-ridge-45.4 docs-only slice at the epic's priority † | 230 | low | `standard-high-opencode-zen` (lane narrow-hosted) | `standard-high-opencode` (lane narrow) |
| fleet-ridge-45.5 provenance refs off refs/heads | 230 | high | `standard-high-opencode-zen` (lane narrow-hosted) | `standard-high-codex` |
| fleet-ridge-45.6 daily backstop branch sweep | 230 | very_high | `standard-high-codex` | `deep-low-codex` — raised from standard-high |
| fleet-ridge-45.7 one-time backlog branch cleanup | 230 | very_high | `fast-high-codex` | `deep-low-codex` — raised from fast-high |
| quilt-trader: order-size rounding fix, narrow + tested | 100 | very_high | `standard-high-opencode` (lane narrow) | `standard-high-codex` |
| quilt-trader: new position-sizing strategy | 100 | very_high | `deep-low-codex` | `deep-low-codex` |
| docs: fix a guide's broken links | 100 | low | `fast-low-opencode` (lane narrow) | `fast-low-opencode` (lane narrow) |
| UI: narrow, test-verified dashboard tweak | 100 | low | `standard-high-opencode` (lane narrow) | `standard-high-opencode` (lane narrow) |
| UI: the same tweak (local OpenCode busy) | 100 | low | `standard-high-opencode-zen` (lane narrow-hosted) | `standard-high-codex` |
| same tweak in shared code (medium risk) | 100 | medium | `standard-high-opencode` (lane narrow) | `standard-high-codex` |
| trivial chore: bump a pinned version | 100 | low | `fast-low-opencode` (lane narrow) | `fast-low-opencode` (lane narrow) |
| trivial chore (local OpenCode busy) | 100 | low | `fast-high-codex` | `standard-high-opencode-zen` (lane narrow-hosted) |
| same chore at priority 200 † | 200 | low | `fast-high-codex` | `fast-low-opencode` (lane narrow) |

What it shows:

- **fleet-ridge-45 leaves the cheap lanes.** Today 45.2, 45.4 and 45.5 go to
  the free hosted model, because priority 230 closes only the local lane. In
  the draft the recovery redesign (45.1), abandon deletion with its audit
  table (45.3), the daily sweep (45.6) and the backlog cleanup (45.7) move to
  `deep-low`, 45.7 up from its `fast-high` hint. 45.2 is very high risk but
  narrow and tested, so the relax rule keeps it at `standard-high` on Codex.
  The provenance ref migration (45.5) and the hand-landing change (45.4) are
  high risk: `standard-high` on Codex. A low-risk docs-only slice stays on
  local OpenCode at any priority, the epic's 230 included.
- **quilt-trader.** A narrow, tested order-path fix goes to local OpenCode
  today; the draft sends it to `standard-high` on Codex. A new strategy was
  already deep and stays there.
- **Low-risk work keeps today's routes** (docs link fix, UI tweak, trivial
  chore) as long as local OpenCode has a free slot.
- **The free hosted lane changes scope.** With local OpenCode busy, today's
  policy spills a low-risk `standard-high` UI tweak to the free hosted model,
  and a trivial chore to Codex. The draft does the reverse: the UI tweak goes
  to Codex, and the trivial chore goes to the hosted model's one
  `standard-high` rung. That is point 5: trivial chores only, until a record
  is earned.
- **Priority does not route.** The same chore at priority 200 routes exactly
  as at the default of 100.

## Validation

Run from the worktree on 2026-10-09:

| Check | Result |
|---|---|
| `scripts/rebuild-reviewed-playbook-artifacts.py --source default-assignment-routing.md --out bundle default-assignment-routing` | compiles; `diagnostics.json` is `[]`; the classify step's `output_schema` requires `risk` (enum) and `risk_reason` |
| `scripts/rebuild-reviewed-playbook-artifacts.py --check` (all shipped bundles) | exit 0, no `DRIFT`: the shipped `default-assignment-routing` still compiles to its recorded artifact |
| Shipped policy digest | `sha256:af27b036…f4cd63e` before and after the mechanism change (a test now pins it) |
| `PYTHONPATH=. python dry-run.py` | both policies parse and plan every example (table above) |
| `aq test` on the routing, route-command, routing-playbook and V2 artifact suites (12 files) | 899 passed |
| `aq test tests/test_integration_size.py tests/test_integration_cutover.py` | 32 and 75 passed |
| `scripts/generate-selection-catalogue.py --check` | current |
| `ruff check` on the changed source, tests and `dry-run.py` | clean (the rebuild script has the same 11 findings as on `main`) |

The 12 `aq test` files: `test_routing_planner`, `test_routing_router`,
`test_task_route`, `test_task_route_source`, `test_assignment_routing`,
`test_default_assignment_routing_playbook`, `test_routing_doctor`,
`test_routing_enforcement`, `test_routing_mandatory`,
`test_default_playbook_v2_artifacts`, `test_required_playbooks`,
`test_playbook_v2_definition`. The mechanism adds 14 tests: 13 in the planner
module, 1 in the router module.

## Open questions for Jack

1. **Which way does priority point?** Routing reads a higher number as more
   important (`local_models.below_priority`, the
   `high_priority_local_model` stall check), but the claim frontier claims
   the lowest number first (`claim_queries.py`, `ORDER BY priority`). The
   draft follows the routing convention: cheap lanes take only priority
   below 150 (local) or 101 (hosted). If lower should mean more important,
   both ceilings need to flip to floors before activation.
2. **Floor for `very_high`: `deep-low` or `deep-high`?** The draft uses
   `deep-low`, the cheapest deep class. `deep-high` Claude stays reserved for
   design and review.
3. **No risk answer.** A failed or missing classification applies no floor
   but keeps every cheap lane closed, so a risky task whose classification
   failed still runs on Claude or Codex at its kind's class. Treating a
   missing answer as `medium` would add a `standard-high` floor; that is one
   mechanism line if you want it.
4. **Track-record numbers.** 10 clean passes, a 14-day look-back, and 2
   reworks in the last 10 to fall back to probation are proposals.

## Decisions

Jack answered the open questions in review `rev-wise-impact` (2026-10-09),
which also made that review's approval the step-3 go-live approval (A1):

1. **Priority does not route.** A task's priority never chooses its model,
   class or lane: no lane and no `local_models` rule names a priority, and the
   planner is not given one. A lane admits work only by its classes and its
   `narrow`, `test_verified` and `max_risk` requirements, so low-risk, narrow,
   test-verified work still takes the cheap lanes, and train repairs stay off
   local models through `local_models.train_kinds`. A policy that still writes
   `below_priority` is refused by name; the `high_priority_local_model` stall
   check is gone with the rule it checked. (Jack first answered that lower
   numbers are more important, as on the claim frontier, then removed priority
   from routing once the filing history showed urgent work numbered 230–298.)
   Two worked examples change: the fleet-ridge docs slice at the epic's 230
   and the chore at 200 now route as at the default priority, to local
   OpenCode (`standard-high-opencode`, `fast-low-opencode`). The other 16
   route as approved.
2. **`very_high` floors at `deep-low`**, relaxed to `standard-high` when
   narrow and test-verified, as drafted.
3. **No risk answer is treated as `medium`.** In a policy that reads risks, a
   classification that answered no risk, or failed, gets the medium floor and
   harnesses; the plan records `class_raised_for_risk.assumed: true`. Before
   the classifier runs, the risk stays unknown and is still asked for.
4. **Track-record numbers stand** as drafted.

## Activating after approval

Approving this document activates nothing. After approval:

1. Land the mechanism and compiler change from this branch, plus the
   `class_raised_for_risk` field on `TaskRoutePlanValue`.
2. Copy the draft source over the shipped reviewed source, the fixture
   (`tests/fixtures/playbooks/v2/default-assignment-routing/`) and the
   default playbook; rebuild with
   `scripts/rebuild-reviewed-playbook-artifacts.py`, update the manifest
   digests.
3. Update the vault copy
   (`system/playbooks/default-assignment-routing.md`, which is currently one
   revision behind the shipped copy), then submit it with
   `aq review submit --playbook-id default-assignment-routing` so approval
   pins exactly the compiled artifact, and activate it.
