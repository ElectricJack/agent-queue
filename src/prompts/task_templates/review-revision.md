Read `aq review show --review-id {{review_id}} --comments`, revise a local draft, and resubmit with `aq review submit --review-id {{review_id}} --file <draft.md> --changes "<comment dispositions and evidence>"`.
Pass `--resolves cmt-...` for each comment you addressed. Do not edit the vault mirror or its frontmatter directly: editing a file alone does not publish a revision.
A passing close requires a newer revision of this review submitted by this task. Record the new revision number and content hash in the close summary; do not wait for approval.
