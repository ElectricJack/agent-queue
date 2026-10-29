# Running the cross-family review loop

Reference for the **decider** seat of the aq-reviews skill
([../SKILL.md](../SKILL.md)): the local operator, or a supervisor the review
was delegated to. Workers never dispatch or decide.

## Contents

- Scope and the installed ladder
- Family diversity is observed, not configured
- Procedure (round checklist and the six steps)
- Rules

## Scope and the installed ladder

For the local operator, or a supervisor the review was delegated to. One
model family writes, another attacks, for **at most three critique rounds**.
It is opt-in: the software-factory policy forbids mandatory multi-pass review
chains, so run it only on a document that warrants it. Workers never dispatch
or decide; `aq review dispatch` answers `operator_only` to them.

**Read the installed ladder**, never recall it from memory:

```bash
aq system list-intelligence-classes
aq agent list-profiles
```

Read profiles and intelligence classes from the install. Class/harness rungs
are derived; their ids and model assignments are not a stable family-selection
API. The project's router chooses candidates from task type and class hints.

## Family diversity is observed, not configured

Family diversity is observed, not configured. Dispatch names no profile:
each reviewer excludes the author provider when attribution is known and the router
picks among the other families; a revision task is unrouted with the
`--responder-class` hint, and the router routes it like any other task.
Failover inside a class can cross families. Unknown author attribution cannot
prove diversity. For every writing and reviewing task, check what actually ran:

```bash
aq task show <task-id>                         # route, provider_intent, rerouted_from
aq task recent-activity --hours 48 --json      # items[].attempts[].model
```

If the required family was unavailable, or a material turn failed over or
reports no model, the round is **inconclusive for cross-family coverage**:
record it so, and never claim coverage you did not observe.

## Procedure

Track each round with this checklist; the numbered steps below give the commands.

```
- [ ] Author task filed by type/class; observed author family recorded
- [ ] Plan comment on the author task (family, cap <= 3 rounds, deadline)
- [ ] Current revision read; that explicit revision dispatched to another family
- [ ] One task wait registered; turn ended
- [ ] Reviewer attribution checked; findings and verdict read
- [ ] request_changes prepared, previewed and decided (or stop and approve)
- [ ] Resubmission attribution, dispositions and content hash recorded
```

1. Before round 1, file the author task with its appropriate type and class
   hints, then inspect the project's route and observed attempts:

   ```bash
   aq task create --project <project> --title "Write spec: <topic>" --description "<brief>" --type design
   ```

   Check the route the router wrote (`aq task show`). If the human requires a
   family the router did not pick, the local operator or the supervisor may
   pin that one task with `aq task route-override --task-id <author-task>
   --profile-id <profile> --reason "..."` on the human's word; otherwise take
   the family that ran and record it.

2. Record the plan — review, the author's observed family, round cap (at most
   three) and deadline (default two working days) — as a comment on the
   author task, and append each round's result there. The revision number is
   not the round counter.

   ```bash
   aq task comment <author-task> --body "Adversarial review plan: author on <observed provider/model>, reviewers exclude its provider, cap 3 rounds, deadline <date>."
   ```

3. Read the current revision from `aq review show --review-id <id>`, then
   dispatch that explicit number to the other family. Round 1 is clean-room;
   later rounds keep the default `--with-comments`.

   ```bash
   aq review dispatch --review-id <id> --revision <n> --class deep-high --no-comments --focus "Verify claims against code; label each finding [blocking] or [nit]"
   ```

4. Register one durable task wait and end the turn:

   ```bash
   aq wait register --kind task --ref <task-id> --idempotency-key review-round-N
   ```

   Resume with `aq wait show <wait-id> --consume --json`. Act on its result or
   recorded supervisor decision; keep the same wait. A failed or cancelled reviewer
   is an incomplete review. Check
   attribution, then read the findings and the verdict:

   ```bash
   aq review show --review-id <id> --revision <n> --comments
   ```

5. For material defects, request changes on that exact revision, naming the
   responder class (a hint; the router routes the revision task):

   ```bash
   aq ask show --ask-id <ask-id> --json
   aq ask prepare --ask-id <ask-id> --expected '<expected JSON from show>' --option-key request_changes --overall-note 'Round 1 of 3: address or explicitly rebut every [blocking] finding' --responder-class <installed-class>
   ```

   Inspect the preview and confirm the same intent within its ten-minute
   lifetime, using the returned preview id and a stable idempotency key:

   ```bash
   aq ask decide --ask-id <ask-id> --expected '<same expected JSON>' --option-key request_changes --overall-note 'Round 1 of 3: address or explicitly rebut every [blocking] finding' --responder-class <installed-class> --preview-id <preview-id> --idempotency-key <unique-key>
   ```

   A compatibility `aq review decide` may return `ask.preview_required`;
   inspect its packet rather than repeatedly issuing the old decision command.
   Only `application_state: applied` confirms the decision's effects completed.
   After the revision task resubmits, check its attribution and record every
   disposition and the new content hash in the round comment.
6. Stop when no unresolved `[blocking]` finding remains **and** the decider
   judges the acceptance criteria adequate; then approve as usual. At the
   round cap or the deadline, stop without approving: leave the review open
   and record the unresolved list. Never approve because the cap was reached.

## Rules

- `duplicate_dispatch` is deduplication working. Pass `--force` only to
  replace a failed or inconclusive round on purpose, and record why.
- `stale_revision`: re-read and act on the current revision.
- `vault_diverged`: `aq review import-edits --review-id <id>` (local operator
  only), or undo the edit. Never overwrite a diverged mirror.
- `aq review delegate` is local-operator only. Do not assume delegation you
  were not given.
- Fresh reviewer context for the first pass where practical. Swap families on
  a different document, never mid-round without recording a new assignment.
- Three rounds is a cap, not a quota.
