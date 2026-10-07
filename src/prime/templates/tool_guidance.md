Use the CLI first; inspect specific commands with `--help` and enums with `aq schema`.
Read authoritative project AGENTS.md/CLAUDE.md, applicable directory instructions and
linked task specs before editing. Profile Role/Rules above remain authoritative.
Discover applicable skills in the harness catalogue and read their SKILL.md before use;
load workflow details on demand (aq-cli, aq-tasks, aq-knowledge,
aq-workspaces-and-git, aq-reviews).
Keep plugins available; avoid reprinting unchanged catalogues, help or transcripts.

Your token is scoped to the held task/session/project. Operator commands (`aq task list`,
`aq doctor`, `aq session`, daemon lifecycle) are out of scope; never bypass a rejection.
Native equivalents include task_show, task_set, task_comment, task_comments, task_close,
task_heartbeat, task_claim, task_handoff, message_send, message_inbox. Use the
convenient surface; both dispatch through CommandHandler.

For durable references, findings, decisions and procedures, use `aq knowledge create`
and `aq record search` with the aq-knowledge skill. Check current `aq record capabilities`
and granted operations for an explicit project, then use scoped lexical search
to avoid duplicates. PostgreSQL owns canonical knowledge; legacy notes, vault
files and Markdown exports do not prove graph ingestion. Optional semantic
memory is separate. Verify a save with its record identity, revision and
`aq knowledge show` readback; guard `aq knowledge update` with the observed `--if-revision`.
Informational record links do not create execution dependencies. A normal save
does not confer verification, policy authority or permission to enable features.
Keep the session's scope; a global supervisor selects an explicit project only
for the targeted operation.

Commit with plain `git` in your own worktree. `aq git commit` is a daemon-side
command unavailable to worker scope; `out of scope: git_commit` is expected.
A local `git commit` is authorized and is not a bypass. For a first publication of a
new task branch or a later fast-forward update, use the guarded `aq git push`. For a
later rewrite, use
`aq git push --expected-remote-oid <your-last-observed-remote-oid>`: that exact lease
must name the remote OID you last observed for your own branch, never another worker's
commit. If the remote moves, escalate; never guess a lease or use plain `git push`.
Never bypass any other AQ rejection.

The daemon injects `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`
and `GIT_COMMITTER_EMAIL` into task and pool worker sessions. New commits use the
project override, else installation default, else `Agent Queue <agent-queue@localhost>`
(`src/git/identity.py`). Keep these variables; do not set an identity yourself or
change Git config. If they are absent, report the launch bug rather than supplying
an identity. Never pass `--no-verify` or amend a pushed commit.

Run focused tests and the related area suite with `aq test`; record exact commands/results.
Keep its worker cap and marker defaults; never raise `-n`. Slot timeout 75 is retryable.
Full-suite runs belong to CI and tasks about the suite. Use the recorded known-failing
baseline instead of capturing your own baseline. A pre-existing failure does not fail
your task or justify changing tests: never weaken or skip a test; name it when closing.
Task authors specify focused/area checks, not "run the full suite before closing".
For agent-queue, see docs/guides/resource-gating.md.

Use durable `aq wait register` with an idempotency key for a supported job, task, message
or timer condition and end the turn; it retains claim/workspace/seat without heartbeat
polling. Register once instead of sleeping, polling process output or rereading an unchanged
inbox. Resume with `aq wait show WAIT_ID --consume --json` (reads the result, consumes only
its notification) and handle the actual failure, cancellation or timeout: a satisfied wait
is not proof of success. Use task waits for review/task settlement, message waits with the
returned thread and cursor for replies, and bounded timers for planned delays; follow
integration-owned CI through its task's settlement, since a timer is not proof CI passed.
Managed tests: `aq test --aq-detach --aq-wait TEST_ARGS` or
`aq job submit --preset test --wait --idempotency-key KEY -- TEST_ARGS` (operator must
enable resources.jobs.enabled; otherwise keep foreground `aq test` resource controls).
Do not resubmit pending work.

If close returns `messages.pending_before_close`, handle each mailbox named in the refusal
with `--inject --json` before retrying; a plain inbox or status read does not consume
delivery. After an accepted close, follow its next-claim result; do not keep polling the
closed task or retrying close.

Summarize normal success with `--brief`. For large evidence, use `--save-output PATH`
on an emit-based CLI command: read the saved JSON by relevant keys/pages as needed.
The receipt gives exact bytes/hash/path; failures, warnings, gates, claim outcomes and
instructions stay visible. Keep full error details and evidence paths in task comments.
Save non-CLI logs to files and page relevant ranges; never replace required instructions
or failures with a success summary.
