# Document reviews

AQ has a review surface for documents — specs and plans first — that lives in
the vault, is decided by Jack, and gates the work that depends on it. A
document is a **review**, not a commit: the worker writes it in its checkout
without committing it, submits it with `aq review submit`, and closes its
task. The daemon stores the text in the database and mirrors it to a vault
file you can read and edit in Obsidian. Approval is a decision on a revision
(`aq review decide`), and any task filed with `--after-review <id>` waits on
the review's gate until you approve.

This page is the operator's guide. The design that defines every word on it
is [the document review design spec](../../superpowers/specs/2026-09-21-document-review-design.md).

## What you get

- **The vault holds the live document.** Every review owns a vault file —
  `projects/<pid>/specs/<date>-<slug>.md` for kinds `spec` and `other`, or
  `projects/<pid>/plans/<date>-<slug>.md` for kind `plan`. The database is the source of truth; the file is an
  atomic copy rewritten whenever the review changes, and re-created if
  missing.
- **The database holds the review.** One row with its state, kind, title,
  revision, current content, authoring task, and the principal allowed to
  decide (`decider`). Every revision keeps its full text, so the dashboard
  can show "changes since the previous revision" and `aq review show
  --revision N` can print an old one.
- **A gate.** An open gate row the scheduler refuses to route anything
  through, linked to the review by `gate_id`, plus a `review_gate` context
  row on the task.

## The lifecycle

```
                  submit
   (no review) ───────────────► in_review
                                 ▲  │
              submit --review-id │  │ decide approve
                                 │  ▼
                          changes_requested ───► approved
                                 │
                                 │ withdraw
                                 ▼
                             withdrawn
```

- `in_review` — waiting for a decision on the current revision.
- `changes_requested` — a decision with feedback; the authoring task is
  reopened (or a revision task filed) with that feedback.
- `approved` — the gate is resolved and dependent tasks become routable.
- `withdrawn` — the review is closed with no decision; the gate is kept open
  so nothing downstream is released, and the authoring task is put back to a
  state where it can resubmit.

Every transition checks the review's state and the revision it was issued
for, so two decisions racing on different revisions cannot both succeed.

## The commands in one table

| Command | You | What |
|---|---|---|
| `aq review list` | yes | Everything that exists, newest first. |
| `aq review show --review-id <id> [--revision N] [--comments]` | yes | Print the current (or a given) revision, and with `--comments` the whole comment thread with each comment's quote, heading path, revision, and resolved state. |
| `aq review decide --review-id <id> --revision N --decision approve --note "…"` | yes | Approve. Resolves the gate, releases dependent tasks, rewrites the vault file. |
| `aq review decide --review-id <id> --revision N --decision request_changes --note "…"` | yes | Reject the revision and attach the note as feedback to the authoring task. |
| `aq review comment --review-id <id> --revision N --body "…" [--quote "…"] [--heading "…"]` | yes | Add one anchored comment without deciding. The author sees it in the thread the next time they `aq review show --comments`. |
| `aq review withdraw --review-id <id> --reason "…"` | yes (`local_operator_only`) | Close the review with no decision. Only you and the original author may withdraw. |
| `aq review delegate --review-id <id> --to supervisor\|user` | yes (`local_operator_only`) | Change `decider` on one review. `supervisor` lets the live named supervisor session approve it. |
| `aq review import-edits --review-id <id>` | yes (`local_operator_only`) | Turn your out-of-band Obsidian edit into revision N+1. Refuses if the current revision is already decided. |

For workers and the supervisor, plus the `aq task create … --after-review
<id>` and `aq task edit … --after-review <id>` the daemon uses to gate work,
see [§ The worker's side](#the-workers-side).

## Who may decide

Only two principals may approve or request changes: **you** (the local
operator, whether you act from the dashboard or a local CLI) and, if you
delegated the review, the live named supervisor session for that project.
Nobody else, by design. The supervisor's `decide` and `comment` are refused
at the API for every session principal before the elevated-session bypass is
considered, so a supervisor cannot grant itself authority.

| Action | You (dashboard or local CLI) | Supervisor | Other agents |
|---|---|---|---|
| `submit`, `show`, `list` | yes | yes | yes (`submit` only from their own task) |
| `withdraw` | yes | yes | the authoring task only |
| `decide`, `comment` | yes | **only when `decider` is `user_or_supervisor`** | no |
| `delegate`, `import-edits` | yes | no | no |

A delegated decision is recorded as `decided_by: "supervisor (delegated)"`
plus the session id, so the audit trail always shows who actually pressed the
button.

**Delegation, set once per project.** You can delegate all new reviews in a
project to the supervisor with `aq project edit --review-delegate-to supervisor`
(revert with `--review-delegate-to user`, the default). That writes the
nullable `projects.review_delegate_to` column (CHECK `user` | `supervisor`)
and sets the initial `decider` of every new review in that project. Only a
local operator may change it; the elevated supervisor is refused
(`local_operator_only`) so it cannot grant itself the setting.

## Edits outside the review

You can edit the vault file directly in Obsidian at any time. On every read
the daemon compares the body hash (frontmatter excluded) with the current
revision's `content_sha256`. If it differs:

- the dashboard shows a banner, *"This file was edited outside the review"*,
  with an **Import my edits** button;
- `aq review decide` is refused with code `vault_diverged`, so you cannot
  approve text that is not the current revision;
- `aq review import-edits --review-id <id>` promotes your edit to revision
  N+1, authored by `jack`; and
- the daemon never overwrites a diverged file. If the file is simply
  missing, `import-edits` rewrites it from the current revision.

## What happens when you reject

Rejection is routed to the author, not dropped:

- **Authoring task still exists and is not running** — the daemon reopens it
  with the feedback (overall note, open-comment count, and the command to
  read the thread) appended to its description, `retry_count` reset, and a
  `reopen_feedback` context row.
- **Authoring task archived or deleted** — the daemon files a revision task
  titled *"Revise \<title> (review \<id>)"* under the original task's parent
  (the project root if the parent is gone), on the same profile and class.
- **Authoring task still IN_PROGRESS** (its worker resubmitted and has not
  closed yet) — the feedback is posted as a task comment, relayed to the
  live worker.
- **No authoring task** (you or the supervisor submitted without `--task-id`)
  — the feedback is sent as a message to the project's supervisor session.

## The worker's side

From the worker's point of view the workflow is short:

1. **Write** the document in the checkout, do not commit it.
2. **Submit** with `aq review submit --task-id <task> --file <draft> --kind
   spec\|plan\|other --title "…"`. The CLI reads the file locally and sends
   the content; the daemon returns the review id and the vault path.
3. **Close** the task as pass with the review id and vault path in the
   summary. The worker does not wait for the decision; the work that depends
   on it waits (via `--after-review`).
4. **Revise** if your task is reopened with feedback: `aq review show
   --review-id <id> --comments`, then resubmit with
   `aq review submit --review-id <id> --file <draft> --changes "…"
   [--resolves <comment-id> …]`, and close again.

This is wired into every shipped worker profile's `## Rules` section as
*Specs and plans go to review, not the repo*, and the shipped supervisor
profile mirrors the operator side (§ Who may decide above). The rule
replaces the superpowers default of committing specs to
`docs/superpowers/specs/`.

Implementers read an approved document with `aq review show --review-id
<id>`; task descriptions cite the review id, not a file path.

## The refusal codes you will see

Every refusal from the review API carries a named code. The ones you are
most likely to meet:

| Code | Meaning | Fix |
|---|---|---|
| `not_your_task` | The reviewer (worker or supervisor) tried to act on a review in a project they do not hold. | Re-run from the session that holds the task, or have the authoring task resubmit. |
| `not_in_review` | A decision was attempted while the review was not in a state that accepts it (or the `--decision` value was not `approve` / `request_changes`). | `aq review show --review-id <id>` to see the actual state, then `withdraw` or wait. |
| `stale_revision` | `--revision` does not match the review's current revision. | `aq review show --review-id <id>` to read the latest, then decide or comment with that revision. |
| `review_closed` | A `submit --review-id` was called after the review had been approved or withdrawn. | Submit a new review (a fresh `--task-id`). |
| `empty` | The file was empty after decoding. | Fill in the draft and retry. |
| `not_utf8` | The file was not valid UTF-8 text. | Convert the source file and retry. |
| `bad_title` / `bad_kind` | `--title` was missing or too short, or `--kind` was not one of `spec`, `plan`, `other`. | Re-run with a sensible title and a known kind. |
| `not_found` | The id does not exist under your principal's scope. | `aq review list` to find the right id. |
| `not_decider` | You tried to `decide` or `comment` on a review whose `decider` does not include your principal. | `aq review delegate --review-id <id> --to user` (if you are Jack) or wait for the supervisor to accept the delegation. |
| `local_operator_only` | A session-scoped principal tried to `delegate` or `import-edits`, or the supervisor tried to set `review_delegate_to`. | Re-run the command as the local operator. |
| `vault_diverged` | The vault file no longer matches the current revision's hash. | `aq review import-edits --review-id <id>` to promote your Obsidian edits, then decide. |
| `review_gate` | The task you filed with `--after-review <id>` hit the open gate and the scheduler refused to route it. | Approve the review, or `aq review withdraw --review-id <id> --reason "…"`. |

## Troubleshooting

Use the consistency check first — `aq doctor --check reviews.consistency` —
because it names the specific repair for every mismatch between the database
and the vault:

- A missing vault file: the check will rewrite it from the current revision
  on `aq doctor --fix reviews.consistency`.
- A diverged vault file: the check reports it, never overwrites. Use
  `aq review import-edits` if the edit is yours, or `aq review show
  --review-id <id>` to copy the approved text back into the file.
- A resolved gate on a non-approved review, or an open gate on an approved
  review: the check reports it and `--fix` repairs the safe direction
  (resolving the gate on an approved review, or flagging the unresolved
  case for you to unwind).
- A task waiting on the gate of a withdrawn review: the check reports it;
  either resubmit the review (new `--review-id`) or `aq task edit
  --after-review ""` on the task to release it from the gate.

## Discord, and what reviews are not

Review submission and revision are announced to the single configured
Discord channel (`config.discord.channel_id`) with the title, kind,
revision, "what changed" note, and a deep link to the review. A revision is
posted at most once — `doc_reviews.notified_revision` tracks the last
announced revision — and the transport retries are fire-and-forget: a
failed send never blocks or fails a submission. Decision is not taken in
Discord.

Reviews are not merges, not gates on *code* changes, and not a replacement
for the escalation flow. A review gates *documents*; the code that
implements an approved document is ordinary work, subject to ordinary
review the same way anything else is. The design spec's out-of-scope list
(§13) is the authoritative boundary if you are tempted to extend reviews to
something else.

## Related pages

- [The document review design spec](../../superpowers/specs/2026-09-21-document-review-design.md)
  — the source for every claim on this page.
- [Escalations and the hourly digest](escalations.md) — the other "human
  in the loop" surface; reviews share its local-operator-vs-supervisor
  principal model.
- [Worker pools](worker-pools.md) — for the pool-vs-task distinction that
  decides whether a worker's rejection-respawn is a pool respawn or a fresh
  assignment.
- [Software-factory policy](../concepts/factory-policy.md) — the broader
  "one decision surface per change" principle that reviews implement.

## Source and tests

`src/reviews/` (service, diff, vault), `src/commands/review_commands.py`
(API handlers and refusal codes), `src/cli/reviews.py` (the hand-written
submit path; every other `aq review …` command is generated from
`src/tools/definitions.py`), `src/models/` (the review and gate rows),
`src/scheduler/` (gate enforcement on routing). Tests live in
`tests/test_review_profile_rules.py` for the profile rule invariants, and
under `tests/` for the service, state machine, permissions, and vault
behaviours — see `tests/` for the full set.
