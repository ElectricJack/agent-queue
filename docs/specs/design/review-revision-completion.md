# Review revision completion

Revision tasks created by `review_decide` carry `review_response` metadata:
the assigned review id and the revision whose feedback they must answer.

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

Worker prime must expose this requirement from the metadata, independent of
task type or description truncation. Instruct the worker to read the review
and comments, revise a local draft, and publish with
`aq review submit --review-id <id> --file <draft.md> --changes "..."`, using
`--resolves` for addressed comments. Workers must not edit the vault mirror
or its frontmatter directly. The close summary records the new revision and
content hash. Publication does not approve the review, and the worker does
not wait for the human decision before closing.
