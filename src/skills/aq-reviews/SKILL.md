---
name: aq-reviews
description: Covers aq document reviews seat by seat — submitting a spec, plan, design proposal or research report (`aq review submit`), answering requested changes on a revision or follow-up task, acting as a dispatched adversarial reviewer with anchored [blocking]/[nit] findings, and, in a reference file, the decider's opt-in cross-family review loop and playbook-policy reviews. Use when a task asks for a spec, plan, proposal or report, when holding an "Adversarial review:", "Revise … (review …)" or "Follow up on … (review …)" task, or when running a cross-family review as the operator or a delegated supervisor.
allowed-tools:
  - Bash
---

# aq reviews

A spec, a plan, a design proposal or a research report is a **review**, not
a commit. The daemon stores every
revision, mirrors the current one to the vault, and gates the work filed with
`--after-review <id>` (a single task, or every node of a `--graph` /
`--from-spec` graph) until the decider approves. The decider is the human
operator, or the supervisor when the operator delegated that review. The operator's guide is
`docs/guides/reviews.md` in the agent-queue repository; this skill is the
seat-by-seat version.

**Reference files** (one level deep):

- [reference/cross-family-loop.md](reference/cross-family-loop.md) — the
  decider's opt-in loop: one model family writes, another attacks, at most
  three rounds.
- [reference/playbook-reviews.md](reference/playbook-reviews.md) — submitting
  a Playbook V2 policy for approval, and what to check when reviewing one.

## Which seat are you in?

| You hold | You are | Read |
|---|---|---|
| A task that asks you to write a spec, plan, proposal or report; any research or design task | author | § Author |
| *Revise \<title> (review \<id>)* | reviser | § Reviser |
| *Follow up on \<title> (review \<id>)* | feedback implementer | § Feedback follow-up |
| *Adversarial review: \<title>* | dispatched reviewer | § Dispatched reviewer |
| The local operator, or the supervisor with the review delegated | decider | [reference/cross-family-loop.md](reference/cross-family-loop.md) |

Reading is open to every seat:

```bash
aq review show --review-id <id>                      # current revision, state, dispatches
aq review show --review-id <id> --revision 2         # one fixed revision
aq review show --review-id <id> --comments           # the thread: quote, heading path, resolved state
aq review show --review-id <id> --diff-from 1        # block diff against an earlier revision
```

## Author

For a spec, use `docs/templates/spec.md` and the **Spec supersession checklist**
in `docs/guides/reviews.md`. Find approved specs it supersedes or overlaps,
including its approved design; cite their approved revisions and approval
evidence. List every predecessor goal as `kept`, `changed` or `dropped`, with
replacement/retained-spec sections and reasons. If none apply, record what you
checked. A removed outcome is a drop even within a changed or deferred goal.
Flag every drop in the submitted review. Each requires the human operator's
explicit sign-off for that goal and replacement revision; record its decision reference and date,
or mark it pending. Pending sign-off blocks approval and affected implementation,
not submission. Delegated approval and silence do not authorize a drop. Ask the
supervisor for inaccessible predecessor evidence rather than guessing.

```
- [ ] Supersession table done (spec only): every predecessor goal kept/changed/dropped
- [ ] Draft written in the checkout, not committed
- [ ] aq review submit succeeded; review id and revision recorded
- [ ] Close summary names review id, vault path and revision.content_sha256
```

1. Write the document in your checkout. Do not commit it.
2. Submit it (`--kind spec`, `plan`, or `other` for a proposal or report):

   ```bash
   aq review submit --task-id <task> --file <draft> --kind spec --title "<title>"
   ```

   Submit even when the task also asks for a committed copy: a document that
   is only on a branch never reaches the Reviews tab.
3. Close the task with the review id, the vault path and the revision's
   content hash (`revision.content_sha256` from `aq review show --review-id
   <id> --json`). Do not wait for the decision; closing approves nothing.

The close gate checks this. A task with a `review` deliverable — and every
research or design task, which carries one (`review`) even when none is
listed — is refused a passing close (`deliverables.unmet`) until it has
submitted a review. When the task honestly produced no document, waive it
visibly: `--deliverable-unmet 'review: <reason>'`. A dispatched reviewer is
exempt; its answer is its comments.

A rejection never reopens your task. The daemon files a separate revision
task, which may land on another worker.

A review that asks approval for a Playbook V2 policy is kind `other` and
attaches the playbook (`--playbook-id`, `--playbook-body`); follow
[reference/playbook-reviews.md](reference/playbook-reviews.md).

## Reviser

The daemon files one *Revise \<title> (review \<id>)* task per
`request_changes`, `fold_in` or legacy `reject` decision. Its description holds the decider's note and every
frozen formal comment with its original id, text, anchor and disposition,
including comments the author previously marked resolved. While the review is
`changes_requested` or `rejected`, only the session holding this task may resubmit (anyone
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

4. Complete every frozen feedback obligation with evidence:

   ```bash
   aq review feedback-complete --task-id <held-task> --obligation-id <id> --evidence "<answer and evidence>"
   ```

   The decision's ask detail lists obligation ids; the task's passing-close
   refusal `feedback.incomplete` also lists pending ids. `--resolves` does not
   complete an obligation. For a *Follow up on …* task, complete its obligations
   too; changing approved requirements needs a separately reviewed amendment.
5. Close the task with the new revision number and content hash.

The daemon resolves an omitted `--review-id` from your held revision task's
assignment. It refuses a different review id with `not_your_task`; never
create a parallel review to work around a resubmission refusal. Report the
refusal and preserve the assigned review.

A passing close is refused with `reviews.revision_unpublished` until this
task publishes a newer revision of its assigned review. Edit a local draft,
not the vault mirror or its frontmatter. An unrelated review or another
task's submission does not meet this requirement, and deliverable waivers
cannot waive it. A failing close remains available. Do not wait for approval
after publication.

`--resolves` marks a comment resolved in the new revision; it does not make a
rebuttal accepted. The decider checks each one, and the next round's reviewer
sees it. `stale_revision` means the review moved: re-read, never bump the
number blindly. `vault_diverged` means someone edited the vault file: report
it; only the operator imports or reverts that edit.

## Feedback follow-up

An approval can create a *Follow up on \<title> (review \<id>)* task for
accepted `follow_up` obligations. Read the frozen acceptance items in its
instructions and record each obligation's evidence with
`aq review feedback-complete --task-id <held-task> --obligation-id <id>
--evidence "<answer and evidence>"` before a passing close. Approved bytes remain
immutable. A change to approved requirements needs a new amendment review;
do not try to resubmit the approved review or treat follow-up completion as a
new approval.

## Dispatched reviewer

Your task was filed by `aq review dispatch`. Its description names the exact
revision you review and whether this is a clean-room read.

- **Read that revision only.** `aq review show --review-id <id> --revision <n>`;
  add `--comments` only when the task says the thread is included. Comments on
  any other revision are refused (`wrong_revision`), and so is commenting from
  any session but the one holding this task (`not_dispatched`).
- **Verify against the repository.** Treat every claim in the document as
  unproven until the code, a test or a command output supports it.
- **Check spec supersession.** Follow the checklist in `docs/guides/reviews.md`:
  independently compare approved predecessor goals with the spec's `kept`,
  `changed`, `dropped` table and its replacement sections. Missing predecessors
  or rows, unsupported dispositions and drops without the human operator's
  explicit sign-off are `[blocking]`. Flag every drop in the review and verdict, including signed-off
  drops, citing the goal, approved revision, reason and decision evidence. A
  deferral or delegated approval cannot replace sign-off. Recheck on revisions;
  sign-off must cover the current proposal. If predecessor evidence is unavailable,
  report the gap rather than claim there are no findings.
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

## Refusals you will meet

| Code | Meaning |
|---|---|
| `not_your_task` | Resubmitting without holding the revision task, or acting on another project's review. |
| `not_dispatched` / `wrong_revision` | Commenting without holding the dispatch task, or on another revision. |
| `anchor_required` | A dispatched reviewer's comment had neither `--quote` nor `--heading-path`. |
| `duplicate_dispatch` | This revision was already dispatched; `--force` dispatches more. |
| `routing.choice_forbidden` | A profile or pin on a filing, a dispatch or a decision; the router routes every task. |
| `operator_only` | Dispatch from a session that is neither the local operator nor elevated. |
| `invalid_responder` / `invalid_responder_class` | Responder options on an approval, or a class that does not exist. |
| `stale_revision` / `not_in_review` | The review moved or is not awaiting a decision; re-read it. |
| `vault_diverged` | The vault file was edited outside the review. |
