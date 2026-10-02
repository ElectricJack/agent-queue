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

Use durable `aq wait register` with an idempotency key for supported conditions and end
the turn; it retains claim/workspace/seat without heartbeat polling. Retrieve the existing
result with `aq wait show WAIT_ID --json`. Managed tests: `aq test --aq-detach --aq-wait TEST_ARGS`
or `aq job submit --preset test --wait --idempotency-key KEY -- TEST_ARGS` (operator must
enable resources.jobs.enabled). Do not resubmit pending work.

Summarize normal success with `--brief`. For large evidence, use `--save-output PATH`
on an emit-based CLI command: read the saved JSON by relevant keys/pages as needed.
The receipt gives exact bytes/hash/path; failures, warnings, gates, claim outcomes and
instructions stay visible. Keep full error details and evidence paths in task comments.
Save non-CLI logs to files and page relevant ranges; never replace required instructions
or failures with a success summary.
