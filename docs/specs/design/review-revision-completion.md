# Review revision completion

Feedback tasks created by `review_decide` carry `review_response` metadata:
the assigned review id, revision and `task_kind` (`revision` or `followup`).
Missing `task_kind` retains the legacy revision-task behavior.

A passing `task_close` must find a persisted revision of that same review,
newer than the assigned revision, with `submitted_task_id` equal to the
closing task. The review must still exist and must not be withdrawn. A new
unrelated review, an older submission, another task's submission, or a vault
file edit alone does not satisfy this gate. Refuse with
`reviews.revision_unpublished` before completion side effects. The refusal
keeps the claim and explains how to resubmit. Deliverable waivers cannot
waive this requirement; a failing close remains available.

Worker `review_submit` resolves the assigned review from the held task's
`review_response` metadata when `review_id` is omitted. An explicit id must
match that assignment; otherwise refuse with `not_your_task` and name the
supported resubmission command. A revision worker cannot create a parallel
review, including by using the ordinary new-document submission command.
Keep the original author, gate and vault path, and attribute the new revision
to the held revision task. Pool tokens resolve the task from the live session
even when their token has no task id. Project boundaries and the existing
assigned-revision/state checks still apply.

A post-approval `followup` task submitting without `review_id` creates a new
amendment review when its predecessor is still approved at the assigned
revision. The amendment has its own author task, gate, vault path and revision
1. Its initial revision's `changes_note` records the predecessor review id,
revision and frozen decision id, together with any submitted changes note.
The approved predecessor, its gate and comments remain unchanged. Explicit
review ids follow the usual authorization for a follow-up worker: it may revise
its own amendment draft, but cannot mutate the approved review or another
task's review. A revision task cannot use approval to escape its assignment.

Follow-up tasks do not require publication of a newer revision of the approved
review at passing close, and prime must not tell them to revise it. Their
feedback obligations and ordinary document deliverables still apply.

Worker prime must expose this requirement from the metadata, independent of
task type or description truncation. Instruct the worker to read the review
and comments, revise a local draft, and publish with
`aq review submit --review-id <id> --file <draft.md> --changes "..."`, using
`--resolves` for addressed comments. Workers must not edit the vault mirror
or its frontmatter directly. The close summary records the new revision and
content hash. Publication does not approve the review, and the worker does
not wait for the human decision before closing.
