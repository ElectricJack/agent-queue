ADVERSARIAL review of {{current_kind}}: {{current_title}}

Read the pinned revision with `{{show_command}}`.
Find defects, unstated assumptions, missing acceptance criteria, and claims unsupported by the code. Verify claims against the repository rather than trusting the document. Agreement is allowed, but be specific.
Post each finding with `aq review comment --review-id {{review_id}} --revision {{revision}} --quote ... --body ...` or use `--heading-path` to anchor a section.
Close this task with a verdict summary: material defects found, or none. Do not decide the review gate.
