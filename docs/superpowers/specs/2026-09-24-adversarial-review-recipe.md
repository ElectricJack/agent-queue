# Adversarial review across model families — preliminary spec

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [mini-projects](2026-09-24-mini-projects.md) ·
[real-time agent collaboration](2026-09-24-realtime-agent-collaboration.md) ·
existing: [document reviews guide](../../guides/reviews.md) ·
[software-factory policy](../../concepts/factory-policy.md) ·
[provider failover](../../specs/provider-failover.md) ·
[hierarchical trains review](2026-09-04-hierarchical-integration-trains-review.md) (a hand-run adversarial review) ·
[planning emits phases and subtasks](2026-09-20-planning-emits-phases-and-subtasks-design.md) (rev. 2 came out of one)

## 1. The ask

"Fable vs. Astra going back and forth on a spec." One model family writes, a
different family attacks it, and the two go around for several revisions. The
operator's priority for this: **recipe only**. It is "doable today via
multiple revisions; mostly a discoverability problem." Write the multi-revision
recipe up as an example playbook, and build a dedicated feature only if users
ask for one.

Names used here:

- **Fable** is `claude-fable-5`, the `anthropic` slice of `deep-low`/`deep-high`
  (`src/prompts/default_intelligence_classes/deep-high.md`). It runs as the
  derived rung `deep-high-claude` on the `claude` harness.
- **Astra** is `gpt-6-astra` in `astra-low`/`astra-high`. It is OpenAI-only and
  runs on the `codex` harness (`astra-high.md`: "OpenAI only (Codex harness)"),
  which makes the rung `astra-high-codex`.

## 2. What exists today

This turns out to be mostly built already. The document review subsystem
already has an adversarial dispatch verb; it just isn't advertised.

**The review state machine** is in `src/reviews/service.py` (`ReviewService`) and
`src/commands/review_commands.py` (`ReviewCommandsMixin`), and the operator
guide is `docs/guides/reviews.md`. A spec is submitted with `aq review submit`
and stored in the DB with every revision. It is mirrored to
`vault/projects/<pid>/specs/…` and gated by a review gate. It moves through the
states `in_review → changes_requested → in_review … → approved | withdrawn`.
Every transition is a CAS on state plus revision.

**`review_dispatch`** is at `review_commands.py:514-664`. It sends a pinned
revision to one or more profiles "for adversarial review":

- It creates one root `research` task per profile, titled
  `Adversarial review: <title>`. The task carries a fixed adversarial brief
  (`:584-602`: "Find defects, unstated assumptions, missing acceptance criteria,
  and claims unsupported by the code. Verify claims against the repository…").
- `--no-comments` asks for a clean-room read, and `--focus "…"` (at most 4000
  characters) steers the reviewer.
- It records a `review_dispatches` ledger row and the `review_dispatch` task
  metadata. Repeating a dispatch for the same (profile, revision) is refused
  with `duplicate_dispatch` unless `--force` is given, so each **new revision
  can be dispatched again without `--force`**.
- It checks that the profile can execute a task (`lifecycle` of `pool` or
  `task`) and holds the `review_show` grant. It warns when a pool has
  `max_active=0`.
- It never changes the review state or the gate.

**Reviewer comments** go through `_cmd_review_comment` (`:451-481`). A worker
may comment only while it holds the matching dispatch task
(`_held_review_dispatch`, `:483-512`), only on that pinned revision
(`wrong_revision`), and must anchor every finding with `--quote` or
`--heading-path` (`anchor_required`). Both worker templates
(`src/profiles/defaults/worker-{claude,codex}/profile.md:79-83`) grant
`review_show`, `review_comment` and `review_submit`, so **every derived rung,
`astra-high-codex` included, can act as a dispatched reviewer**.

**Revision loop.** `review_decide --decision request_changes --note …` routes
the feedback back to the author (guide §"What happens when you reject"). A
live, not-running authoring task is reopened with the note and the count of
open comments. If the author is archived, `_on_review_changes_requested`
(`:698-777`) files `Revise <title> (review <id>)`, listing every unresolved
inline comment with its anchor, on `responder_profile`/`responder_class` if
the decider passed them (the tool schema is at `src/tools/definitions.py:6438`)
or otherwise the project default. The reviser resubmits with
`aq review submit --review-id <id> --file … --changes … --resolves cmt-…`.
`_cmd_review_submit` (`:151-212`) accepts a resubmission only from the
`review_response` task, or from the author while the review is still
`in_review`.

**Who can drive it.** The local operator can do everything. The supervisor
profile holds `review_dispatch`/`review_decide`
(`supervisor/profile.md:129-137`), but it may only decide or comment when
`decider = user_or_supervisor`. That is set per review with
`aq review delegate --to supervisor`, or per project with
`aq project edit --review-delegate-to supervisor`. Workers cannot dispatch or
decide.

**Model-family pinning.** Provider intent is defined in `src/providers/intent.py`.
An explicit `--profile` is `preferred` and **fails over**, and only a
human, an elevated supervisor, a vault formula or a reviewed playbook may set
`pinned`. `review_dispatch` calls `_cmd_create_task` with a bare `profile_id`,
so the reviewer task is `preferred`. For Astra this is harmless: the class has
no non-OpenAI slice, so failover can only hold. For a **Fable author** it is
not harmless. `deep-high-claude` can be rerouted to `deep-high-codex`
(`gpt-5.6-sol`) during an Anthropic outage (`src/providers/reroute.py`), and
then both sides are on the same family without anyone noticing.

**Events** (`src/event_schemas.py:806-844`): `review.submitted`,
`review.revised`, `review.decided`, `review.commented`, `review.withdrawn`,
plus `review.dispatched`, which is emitted at `review_commands.py:652` but has
no `_REVIEW_SCHEMAS` entry (unverified whether that matters for playbook
triggers). A V2 rule can trigger on any registered type
(`src/playbooks/validation.py:1358-1366`).

**Prior art done by hand.** `2026-09-04-hierarchical-integration-trains-review.md`
("Stance: Adversarial") and its design's §18 "Adversarial review disposition",
`2026-09-01-playbook-v2-semantic-graph-design.md` ("revised after adversarial
review"), and the planning spec's rev. 2 all ran this loop manually, with the
review written as a sibling markdown file.

**Policy constraint.** The supervisor profile (`:305-309`) and the factory
policy forbid *mandatory* multi-pass review chains ("a review is one explicit
task or gate when a change warrants it"). The recipe must therefore stay
opt-in.

## 3. Gaps

1. **Discoverability.** The only place `review dispatch` is documented is one
   table row in `docs/guides/reviews.md`. No `src/skills/aq-*` skill mentions it
   (checked with a grep of `src/skills`). The dashboard has no dispatch control
   (no `review_dispatch` call in `dashboard/src` outside tests). Nothing
   explains how to run "author on family A, reviewer on family B, N rounds".
2. **No round cap or stop signal.** The reviewer's verdict is free text in its
   close summary ("material defects found, or none"). Comments carry no
   severity. "Converged" is a human judgement from reading
   `aq review show --comments`.
3. **Author family is not pinned by default.** See the failover note above.
   Revision tasks also inherit `class_only`/`preferred`
   (`_on_review_changes_requested` sets `provider_intent` to
   `"preferred" if selected_profile else "class_only"`).
4. **A playbook cannot drive the loop as things stand.** `review_dispatch`,
   `review_decide` and `review_show` are **not contracted commands**. None of
   them appears in `src/commands/contracts/*.py`, so a V2 `command` step cannot
   call them. An `agent_task` step can start a reviewer, but that reviewer is
   not in the dispatch ledger, so its `review_comment` calls fail with
   `not_dispatched`. A playbook principal is also not a permitted decider.
5. **The reopen path and `responder_profile`.** It is unverified whether
   `responder_profile`/`responder_class` affect the *reopen* path or only the
   filed revision task. If only the latter, a reopened author keeps whatever
   profile it had.

## 4. Implementation options

### Option A — Documentation recipe only (the operator's default)

Write a new section in `docs/guides/reviews.md` titled "Adversarial review
across model families". Mirror a short version into `src/skills/aq-tasks` or a
new `aq-reviews` skill so workers and the supervisor find it. The recipe, as it
can be run today:

```bash
# 0. Optional: let the supervisor run the loop.
aq review delegate --review-id <id> --to supervisor   # or project-wide --review-delegate-to

# 1. Author on Fable, pinned so it cannot fail over to another family.
aq task create -p <pid> -t "Draft spec: <topic>" --profile deep-high-claude --pin \
  -d "Write the spec … submit with aq review submit --kind spec; close with the review id."

# 2. Each round r = 1..N (N = 3 suggested):
aq review dispatch --review-id <id> --to astra-high-codex [--no-comments on r=1] \
  --focus "Round r: verify every code claim; list blocking vs. nit"
#    wait for the dispatch task to close; read its verdict + anchored comments:
aq review show --review-id <id> --comments
#    stop if verdict == "no material defects" or r == N; otherwise:
aq review decide --review-id <id> --revision <r> --decision request_changes \
  --note "Address Astra round r findings; mark each cmt-… resolved or rebut in --changes" \
  --responder-profile deep-high-claude

# 3. Approve (human, or supervisor if delegated).
aq review decide --review-id <id> --revision <final> --decision approve --note "…"
```

Suggested conventions for the recipe to spell out:

- Round 1 is clean-room (`--no-comments`), and later rounds see the thread.
- The reviewer prefixes each comment body with `[blocking]` or `[nit]`.
- The author may *rebut* a finding in `--changes` rather than make the edit.
- Cap the loop at 3 rounds, then a human decides.
- For symmetry, swap roles on a second document so that Astra authors and
  Fable reviews.

- **Touches:** docs plus one skill file. No code.
- **Pros:** zero risk. It fits the factory policy. It uses exactly the audited
  paths (ledger, anchored comments, CAS decisions).
- **Cons:** someone has to drive every round by hand, the operator or a
  delegated supervisor. The stop signal is still prose.
- **Size:** S.

### Option B — Supervisor-driven recipe (A plus a prompt)

Everything in A, plus a short "adversarial review loop" rule in the supervisor
profile (or a skill it loads): "when asked for an adversarial review of review
`<id>`, dispatch to the opposite family, wait for the task, decide
`request_changes` or stop, and cap at N rounds". This needs delegation
(`decider=user_or_supervisor`), which is the operator's explicit, per-review or
per-project opt-in. The supervisor is woken by `review.*` events only
indirectly, through its patrol or inbox, so rounds advance at patrol cadence
(about 15 minutes) unless the operator nudges it.

- **Touches:** `src/profiles/defaults/supervisor/profile.md` (a shipped-profile
  change, which reaches existing installs only through
  `aq agent profile-reseed`), plus docs.
- **Pros:** hands-off after one instruction. No new code.
- **Cons:** adds more prose to an already long supervisor prompt. Round latency
  follows patrol cadence. The approve decision is still the supervisor's
  judgement.
- **Size:** S.

### Option C — Shipped example playbook (reviewed bundle, opt-in)

This is a project-scoped V2 playbook `adversarial-review`, shipped like
`ci-main-sentinel` (fixture under `tests/fixtures/playbooks/v2/`, copy under
`src/prompts/reviewed_playbooks/`, never auto-activated). Rough outline, which
**requires the small code change in step 0**:

0. **Contract `review_dispatch`**, with outcomes `dispatched` /
   `duplicate_dispatch` / `review_closed` / `rejected`, in
   `src/commands/contracts/builtin.py`. It should probably pass
   `provider_intent: pinned` when the caller is a reviewed playbook.
   Optionally contract a read-only `review_status` that returns the latest
   dispatch verdict and the counts of open and blocking comments.
1. **Trigger:** `review.submitted` filtered on `kind: spec` plus a label or
   title convention (open question 3), and `review.revised` for later rounds.
2. `command review_dispatch` → `to: [astra-high-codex]`,
   `revision: event.revision`, `with_comments: event.revision > 1`.
3. `wait` of kind `task` on the dispatch task id (the ledger gives it back)
   → `completed | failed | cancelled`.
4. `llm` step (small class, `output_schema {verdict: converged|revise, blocking: int}`)
   classifies the reviewer's close summary and the comment count. Or use a
   `decision` on `review_status` if that gets contracted.
5. `decision`:
   - `converged` or `event.revision >= N` → a `wait` of kind `human`
     (outcomes `approve`/`revise_more`), or just a terminal that leaves the
     gate for the human;
   - `revise` → call `review_decide request_changes`. That needs a
     **decider principal for reviewed playbooks** (new), or instead a
     `message_send` (already contracted, `builtin.py:1160`) to the supervisor
     asking it to decide.
6. Nothing loops inside the run. The next round is a **new run** triggered by
   `review.revised`, which keeps each run acyclic and short. Round state lives
   in the review revision number.

- **Touches:** contracts (1 or 2 commands), possibly a playbook-as-decider
  rule in `_review_decider_label`, the reviewed bundle plus manifest digests,
  and docs.
- **Pros:** it becomes discoverable in the playbook catalog and graph view,
  it is auditable per round (run receipts), and model pinning is enforced by
  the artifact.
- **Cons:** it is feature work the operator said to defer. Letting a playbook
  be a review decider is a trust-model change that runs against "decided by
  Jack" (guide §"Who may decide").
- **Size:** M (step 0 plus the bundle). L if the decider change is included.

### Option D — Dedicated "adversarial review" feature

This is only if users ask. `aq review adversarial --review-id <id> --author-profile … --reviewer-profile … --rounds N`
would add a first-class loop record, severity-typed comments, a convergence
rule, and a dashboard control.

- **Size:** L–XL.

## 5. Initial take

*Provisional.* Do **A now**, fold **B** in with it if the operator is happy
to delegate reviews to the supervisor, and write C as prose in the guide
("how you would automate this") without shipping the bundle. The primitives
already exist and are well-fenced: the dispatch ledger, anchored comments,
per-revision dispatch without `--force`, and responder routing on
`request_changes`. The honest gaps are discoverability and one real
correctness hazard, which is **failover silently collapsing the two families
into one**. The recipe covers that with `--pin` on the author and
`--responder-profile` on each decision. It is worth a one-line doc warning,
and possibly a small fix so that `review_dispatch` can pin (open question 2).
Promote to C only if the operator or users actually run the recipe and find
the manual rounds painful.

## 6. Open questions

1. **Does the recipe belong in the reviews guide, a new `aq-reviews` skill, or
   both?** This decides whether workers and the supervisor, and not only
   humans, discover it. The skills are what agents actually read.
2. **Should `review_dispatch` accept `--pin`/`provider_intent`?** This decides
   whether a reviewer can be guaranteed to stay on its family. It matters for
   the Fable-as-reviewer direction, because `deep-high-claude` fails over to
   `gpt-5.6-sol`.
3. **What marks a review as "adversarial-eligible"** for a playbook trigger
   (kind, a label on the author task, a review field)? `review.submitted`
   carries only `title`/`kind`/`revision`/`author_task_id`. This only matters
   if C is pursued.
4. **Do comments need a severity** (`blocking`/`nit`), or is a body-prefix
   convention enough? This decides whether "converged" can be computed rather
   than judged.
5. **Does `responder_profile` apply when the author task is *reopened*,** or
   only when a revision task is filed? If only the latter, the recipe's
   `--responder-profile` does nothing in the common case and the author pin
   has to carry the family on its own. This needs a code read of the service's
   `changes_requested` hook.
6. **May a reviewed playbook ever be a review decider?** Today only the human
   and a delegated live supervisor can decide. Any C that loops unattended
   needs an answer here, and the default answer should probably be "no".
7. **Should rounds run with the thread or clean-room?** This decides the
   default `--no-comments` policy per round. A clean-room read catches
   anchoring on earlier findings but repeats them.
8. **Symmetric mode:** should the recipe alternate the author and reviewer
   families between documents, or within one document? Within one document
   that would mean changing the reviser's profile mid-review, which may
   confuse the author's context.

## 7. Dependencies and sequencing

- Option A has **no dependencies**.
- B depends on the operator's choice about delegation (an existing flag) and a
  supervisor profile reseed on existing installs.
- C depends on contracting `review_dispatch` (small) and, for full automation,
  the decider question (6). It would also benefit from the
  [agent sleep/wake](2026-09-24-agent-sleep-wake.md) work only indirectly,
  since each round is a fresh task.
- The [mini-projects](2026-09-24-mini-projects.md) spec can reuse this recipe
  as its "critique" phase. Settle the recipe's conventions (severity prefix,
  round cap) first so both use one vocabulary.

## 8. Non-goals

- Adversarial review of **code** (PRs and diffs). Reviews gate documents
  only, per the guide's §"what reviews are not". Code review stays with
  integration.
- Making adversarial review mandatory or default for any document (factory
  policy).
- Live, turn-by-turn debate between two sessions. That is
  [real-time collaboration](2026-09-24-realtime-agent-collaboration.md).
- A general "N models vote" ensemble feature.
