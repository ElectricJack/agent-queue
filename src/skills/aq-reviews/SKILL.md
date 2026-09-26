---
name: aq-reviews
description: Document reviews in aq — submitting a spec or plan, answering requested changes on a revision task, acting as a dispatched adversarial reviewer, and the opt-in recipe for adversarial review across model families (author on one family, reviewer on another, at most three rounds, [blocking]/[nit] findings, fixed/rebutted/deferred dispositions). Use when you submit or revise a review, hold an "Adversarial review:" or "Revise … (review …)" task, or run a cross-family review loop as the operator or a delegated supervisor.
allowed-tools:
  - Bash
---

# aq reviews

A spec or plan is a **review**, not a commit. The daemon stores every
revision, mirrors the current one to the vault, and gates the work filed with
`--after-review <id>` until the decider approves. The decider is Jack, or the
supervisor when he delegated that review. The operator's guide is
`docs/guides/reviews.md` in the agent-queue repository; this skill is the
seat-by-seat version.

## Which seat are you in?

| You hold | You are | Read |
|---|---|---|
| A task that asks you to write a spec or plan | author | § Author |
| *Revise \<title> (review \<id>)* | reviser | § Reviser |
| *Adversarial review: \<title>* | dispatched reviewer | § Dispatched reviewer |
| The local operator, or the supervisor with the review delegated | decider | § Running the cross-family loop |

Reading is open to every seat:

```bash
aq review show --review-id <id>                      # current revision, state, dispatches
aq review show --review-id <id> --revision 2         # one fixed revision
aq review show --review-id <id> --comments           # the thread: quote, heading path, resolved state
aq review show --review-id <id> --diff-from 1        # block diff against an earlier revision
```

## Author

1. Write the document in your checkout. Do not commit it.
2. Submit it:

   ```bash
   aq review submit --task-id <task> --file <draft> --kind spec --title "<title>"
   ```

3. Close the task with the review id, the vault path and the revision's
   content hash (`revision.content_sha256` from `aq review show --review-id
   <id> --json`). Do not wait for the decision; closing approves nothing.

A rejection never reopens your task. The daemon files a separate revision
task, which may land on another worker.

## Reviser

The daemon files one *Revise \<title> (review \<id>)* task per
`request_changes` decision. Its description holds the decider's note and every
unresolved anchored comment with its id. While the review is
`changes_requested`, only the session holding this task may resubmit (anyone
else gets `not_your_task`).

1. Read the whole thread: `aq review show --review-id <id> --comments`.
2. Revise the draft, and give **every** finding a disposition:
   - `fixed` — the document changed; say where.
   - `rebutted` — the finding is wrong; show the evidence (`path:symbol`).
   - `deferred` — for a `[blocking]` finding, only when the document itself
     now states the exclusion and updates its acceptance criteria. A deferral
     the document does not record is still an open finding.
3. Resubmit with one `--changes` entry per comment (id, disposition,
   rationale, evidence) and a `--resolves` for each comment you answered:

   ```bash
   aq review submit --review-id <id> --file <draft> --changes "cmt-1a2b fixed: section 4 now names the dedup key. cmt-3c4d rebutted: the handler refuses that case (review_commands.py:_cmd_review_decide)." --resolves cmt-1a2b --resolves cmt-3c4d
   ```

4. Close the task with the new revision number and content hash.

`--resolves` marks a comment resolved in the new revision; it does not make a
rebuttal accepted. The decider checks each one, and the next round's reviewer
sees it. `stale_revision` means the review moved: re-read, never bump the
number blindly. `vault_diverged` means someone edited the vault file: report
it; only the operator imports or reverts that edit.

## Dispatched reviewer

Your task was filed by `aq review dispatch`. Its description names the exact
revision you review and whether this is a clean-room read.

- **Read that revision only.** `aq review show --review-id <id> --revision <n>`;
  add `--comments` only when the task says the thread is included. Comments on
  any other revision are refused (`wrong_revision`), and so is commenting from
  any session but the one holding this task (`not_dispatched`).
- **Verify against the repository.** Treat every claim in the document as
  unproven until the code, a test or a command output supports it.
- **Anchor every finding** with `--quote "<exact text>"` or
  `--heading-path '["<section>","<subsection>"]'` (else `anchor_required`).
- **Lead with a severity**, then give the claim, the evidence (`path:symbol`
  and the revision), a concrete failure scenario, and the correction asked for
  or a viable alternative:
  - `[blocking]` — a contradiction, a missing invariant, an unsafe failure
    path, a claim the code does not support, or an acceptance criterion that
    cannot be tested.
  - `[nit]` — wording or readability.

  ```bash
  aq review comment --review-id <id> --revision <n> --quote "<exact text>" --body "[blocking] Claim: ... Evidence: src/x.py:func at revision <n> ... Failure: ... Fix: ..."
  ```

- **Later rounds** review the whole revised document plus the author's
  dispositions, not only the diff. A rebuttal you do not accept gets a new
  `[blocking]` comment that cites the earlier comment id and says why.
- **Do not manufacture defects.** "No material defects" is a valid verdict
  when it names what you checked.
- **Close with the verdict**, and never decide the gate:

  ```bash
  aq task close <task-id> --outcome pass --summary "Verdict: 2 blocking (cmt-..., cmt-...), 1 nit. Checked: sections 2-4 against src/..., acceptance criteria in section 5."
  ```

  `pass` means the review was carried out, whatever it found. If you could
  not read the revision or verify its claims, close `fail` and say why: an
  incomplete review is not "no findings".

## Running the cross-family loop

For the local operator, or a supervisor the review was delegated to. One
model family writes, another attacks, for **at most three critique rounds**.
It is opt-in: the software-factory policy forbids mandatory multi-pass review
chains, so run it only on a document that warrants it. Workers never dispatch
or decide; `aq review dispatch` answers `operator_only` to them.

**Pick rungs from the installed ladder**, never from memory:

```bash
aq system list-intelligence-classes
aq agent list-profiles
```

On the shipped ladder Fable is `deep-high`'s `anthropic` slice
(`deep-high-claude`) and Astra is the OpenAI-only `astra-high`
(`astra-high-codex`). An install whose `deep-high` OpenAI slice is
`gpt-6-astra` has Astra on `deep-high-codex`; the commands below assume that.

**Family diversity is observed, not configured.** Dispatch has no `--pin`
(the reviewer task is `preferred`); a revision task is `preferred` with
`--responder-profile`, `class_only` without; an author task's `--pin` does not
carry over to later tasks. Failover inside a class crosses families: an
Anthropic outage can move `deep-high-claude` work to `deep-high-codex`. So for
every writing and reviewing task, check what actually ran:

```bash
aq task show <task-id>                         # provider_intent, rerouted_from, reroute
aq task recent-activity --hours 48 --json      # items[].attempts[].model
```

If the required family was unavailable, or a material turn failed over or
reports no model, the round is **inconclusive for cross-family coverage**:
record it so, and never claim coverage you did not observe.

**Procedure.**

1. Record the plan — review, author and reviewer rungs, round cap (at most
   three) and deadline (default two working days) — as a comment on the
   author task, and append each round's result there. The revision number is
   not the round counter.

   ```bash
   aq task comment <author-task> --body "Adversarial review plan: author deep-high-claude, reviewer deep-high-codex, cap 3 rounds, deadline <date>."
   ```

2. Before round 1, file the author task on the author's rung, with `--pin`
   when the family is a requirement:

   ```bash
   aq task create --project <project> --title "Write spec: <topic>" --description "<brief>" --profile deep-high-claude --pin
   ```

3. Read the current revision from `aq review show --review-id <id>`, then
   dispatch that explicit number to the other family. Round 1 is clean-room;
   later rounds keep the default `--with-comments`.

   ```bash
   aq review dispatch --review-id <id> --revision <n> --to deep-high-codex --no-comments --focus "Verify claims against code; label each finding [blocking] or [nit]"
   ```

4. Wait for the reviewer task to end (`aq task show <task-id>`). A failed or
   cancelled reviewer is an incomplete review. Check attribution, then read the
   findings and the verdict:

   ```bash
   aq review show --review-id <id> --revision <n> --comments
   ```

5. For material defects, request changes on that exact revision, naming both
   the responder class and profile:

   ```bash
   aq review decide --review-id <id> --revision <n> --decision request_changes --note "Round 1 of 3: address or explicitly rebut every [blocking] finding" --responder-class deep-high --responder-profile deep-high-claude
   ```

   After the revision task resubmits, check its attribution and record every
   disposition and the new content hash in the round comment.
6. Stop when no unresolved `[blocking]` finding remains **and** the decider
   judges the acceptance criteria adequate; then approve as usual. At the
   round cap or the deadline, stop without approving: leave the review open
   and record the unresolved list. Never approve because the cap was reached.

**Rules.**

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

## This loop is not a playbook

`review_dispatch`, `review_show` and `review_decide` are not contracted
commands, so a V2 `command` step cannot call them. An `agent_task` reviewer is
outside the dispatch ledger, so its comments are refused `not_dispatched`.
`review.dispatched` has no event schema, so a rule on it fails validation
(`unknown_event`). A playbook principal cannot decide. Do not write a playbook
that pretends otherwise; contracting the verbs is a separate feature.

## Refusals you will meet

| Code | Meaning |
|---|---|
| `not_your_task` | Resubmitting without holding the revision task, or acting on another project's review. |
| `not_dispatched` / `wrong_revision` | Commenting without holding the dispatch task, or on another revision. |
| `anchor_required` | A dispatched reviewer's comment had neither `--quote` nor `--heading-path`. |
| `duplicate_dispatch` | That profile already has this revision. |
| `operator_only` | Dispatch from a session that is neither the local operator nor elevated. |
| `invalid_responder` / `invalid_responder_class` / `invalid_responder_profile` | `--responder-profile` without `--responder-class`, responder options on an approval, or a class or worker that does not exist. |
| `stale_revision` / `not_in_review` | The review moved or is not awaiting a decision; re-read it. |
| `vault_diverged` | The vault file was edited outside the review. |
