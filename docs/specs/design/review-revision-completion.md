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

Worker prime must expose this requirement from the metadata, independent of
task type or description truncation. Instruct the worker to read the review
and comments, revise a local draft, and publish with
`aq review submit --review-id <id> --file <draft.md> --changes "..."`, using
`--resolves` for addressed comments. Workers must not edit the vault mirror
or its frontmatter directly. The close summary records the new revision and
content hash. Publication does not approve the review, and the worker does
not wait for the human decision before closing.
