State one verified condition with `aq wait register --task PRODUCER_TASK
--idempotency-key settlement` (or `--condition` JSON), then end the turn. AQ chooses
sleep in place or hibernation, maintains ownership and delivers the outcome. If the
receipt requests a checkpoint, submit `aq wait checkpoint` with the compaction summary
and end the turn. Resume by acknowledging the outcome with `aq wait show WAIT_ID
--consume --json`; availability never proves producer success. Message conditions use
the returned thread and cursor. Human conditions have no deadline and resume only on
verified human evidence or a supervisor decision. Escalations reach the supervisor;
workers receive `cancelled`, `source_unavailable`, `lifted_by_supervisor` or `satisfied`.
A supervisor may extend, reroute, cancel or wake through `aq wait decide`, or relay a
verified response through `aq wait lift`. These operations are out of scope for
workers. Keep the same wait and its original age.
Managed tests: `aq test --aq-detach --aq-wait TEST_ARGS` or
`aq job submit --preset test --wait --idempotency-key KEY -- TEST_ARGS` (operator must
enable resources.jobs.enabled; otherwise keep foreground `aq test` resource controls).
Do not resubmit pending work.
