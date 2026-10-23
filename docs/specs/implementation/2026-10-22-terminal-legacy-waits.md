# Terminal legacy waits without a live session

The agent-waits bridge treats a legacy `satisfied`, `expired` or `cancelled`
wait whose session is stopped or missing as settled historical data. Under the
existing graph/session/wait locks, adoption returns successful disposition
`terminal_session_unavailable` with the legacy state, session identity and
observed session state. The migration pass records this evidence in a `complete`
per-object receipt, replacing any earlier `held` receipt. When no other holds or
new candidates remain and the scheduler audit succeeds, the phase receipt is
`complete`, so daemon startup skips the project.

This disposition preserves the legacy row, outcome, digest and result message.
It creates no verified wait or continuation, delivers no message and changes no
task or claim ownership. It does not assert that the underlying source succeeded.
Replays return the same disposition. Preview and disabled application remain
read-only. Active waits still require their current live owner, and terminal
waits belonging to live sessions retain the existing adoption and source checks.
