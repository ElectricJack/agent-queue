{{facts}}

## What to do
Make `{{ref}}` green again with the smallest change that does it. Reproduce the
failure at the head sha above first; if it does not reproduce, say so in the close
summary with the evidence and do not change code. Fix the actual defect, not the
test, unless the test is asserting something that is no longer true. Land the fix
through a pull request against the default branch; never push to it directly.
If this project uses an integration train, submit the repair PR for its normal
review and train collection; do not merge the repair PR yourself. The train owns
candidate CI, bounded repair, and promotion. Do not create a parallel merge path.
Do not bundle unrelated work. If the failure is outside this repository's control
(a runner outage, a rate limit), close with outcome fail and failure-class hard
and say why, so the sentinel escalates to a human instead of filing another attempt.

## Acceptance criteria
{{acceptance}}
- The change touches only what the failure required
- This is attempt {{attempt}} for this failure signature