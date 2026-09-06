# Task11a fix round1 review — 1f54381e

/root/review_11a_astra: all5 original Important findings ADDRESSED and warning Minor
documented, but two new Important regressions. Spec/quality Needs fixes.

## New Important findings (verbatim)

1. **The identity validator rejects valid modern GitHub App client IDs.**
`src/config.py:1469` requires the literal prefix `Iv1.` for both positive and negative identities. GitHub’s official documentation provides the valid example `Iv23f8doAlphaNumer1c`, which this pattern rejects. This prevents loading legitimate operator credentials and propagates into the editor schema. Support both documented formats while retaining secret-safe validation, and add positive tests for each. [GitHub’s client-ID announcement](https://github.blog/changelog/2024-05-01-github-apps-can-now-use-the-client-id-to-fetch-installation-tokens/)

2. **Successfully completed parents acquire false missing-receipt blockers.**
`src/integration/status.py:428` selects only active/escalated/human-required operations. After successful completion, the existing completion path preserves the checkpoint and records the operation as `completed` (`src/integration/parent_completion.py:1097`). The new selector therefore skips shared readiness at line 444 and falls through to line 496, reporting every terminal child as lacking a current collection despite valid delivered receipts. Resolve the operation by the checkpoint’s episode, including completed history; retain the active-only filter separately for repair reporting. Add a status assertion immediately after successful parent completion, before starting another episode.

## Addressed evidence

Value validation pre/post substitution1681, schema232; real SQLite BEGIN81 and
intervening-write test333; shared ParentCompletion.readiness_on445; typed budgets647;
DML rejection observer test313. Warning categories in report488.
No tests rerun, edits, Git, network or operator mutations. Controller supplied primary
GitHub format evidence; reviewer checked completed-operation persistence seam.
