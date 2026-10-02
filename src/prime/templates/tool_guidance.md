Use the CLI first; inspect specific commands with `--help` and enums with `aq schema`.
Read authoritative project AGENTS.md/CLAUDE.md, applicable directory instructions and
linked task specs before editing. Profile Role/Rules above remain authoritative.
Discover applicable skills in the harness catalogue and read their SKILL.md before use;
load workflow details on demand (aq-cli, aq-tasks, aq-workspaces-and-git, aq-reviews).
Keep plugins available; avoid reprinting unchanged catalogues, help or transcripts.

Your token is scoped to the held task/session/project. Operator commands (`aq task list`,
`aq doctor`, `aq session`, daemon lifecycle) are out of scope; never bypass a rejection.
Native equivalents include task_show, task_set, task_comment, task_comments, task_close,
task_heartbeat, task_claim, task_handoff, message_send, message_inbox, memory_save,
memory_search. Use the convenient surface; both dispatch through CommandHandler.

Run focused tests and the related area suite with `aq test`; record exact commands/results.
Keep its worker cap and marker defaults; never raise `-n`. Slot timeout 75 is retryable.
Full-suite runs belong to CI and tasks about the suite. Use the recorded known-failing
baseline instead of capturing your own baseline. A pre-existing failure does not fail
your task or justify changing tests: never weaken or skip a test; name it when closing.
Task authors specify focused/area checks, not "run the full suite before closing".
For agent-queue, see docs/guides/resource-gating.md.

For a supported job, task, message or timer condition, use `aq wait register` with
an idempotency key and end your turn. An active durable wait retains your claim,
workspace and pool seat without heartbeat turns. Resume from the result pointer
with `aq wait show WAIT_ID --consume --json`: this reads the result and consumes only
its notification. Handle the actual failure, cancellation or timeout; a satisfied wait
does not by itself prove success. Register once and end the turn instead of sleeping,
polling process output, or repeatedly reading an unchanged inbox. Registration returns
immediately. For managed validation, use
`aq job submit --preset test --wait --idempotency-key KEY -- TEST_ARGS` or
`aq test --aq-detach --aq-wait TEST_ARGS`; submission and its wait commit
together. Job admission requires the operator to enable `resources.jobs.enabled`.
Use task waits for review or task settlement, message waits with the returned thread
and cursor for replies, and bounded timers for planned delays. CI owned by integration
is followed through its task's settlement; a timer is not proof that CI passed. If managed
admission is disabled, keep the normal foreground `aq test` resource controls.

If close returns `messages.pending_before_close`, read and handle each mailbox named
in the refusal with `--inject --json` before retrying. A plain inbox or status read
does not consume delivery. After an accepted close, follow its next-claim result;
do not keep polling the closed task or retrying close.
Do not resubmit pending work.

Summarize normal success with `--brief`. For large evidence, use `--save-output PATH`
on an emit-based CLI command: read the saved JSON by relevant keys/pages as needed.
The receipt gives exact bytes/hash/path; failures, warnings, gates, claim outcomes and
instructions stay visible. Keep full error details and evidence paths in task comments.
Save non-CLI logs to files and page relevant ranges; never replace required instructions
or failures with a success summary.
