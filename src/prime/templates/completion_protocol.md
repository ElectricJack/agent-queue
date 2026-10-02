Close explicitly when work is complete:

    aq task close {task_id} --outcome pass|fail --summary "Changes, findings, checks, issues"
    aq session drain-ack

## Deliverable self-check

Reconcile every required file, symbol, test, command and registration before a passing
close. Record repeatable checks with `--test` / `--command`; unavailable checks are not
passes. Test deliverables must name the actual commands/files. Submit required documents
with `aq review submit --task-id {task_id} --file <draft.md> --kind <kind> --title "<title>"`;
research/design work carries a review deliverable even when not listed. Declare intentional
gaps with `--deliverable-unmet 'id: reason'`; undeclared gaps refuse close. Follow the
aq-tasks/aq-reviews skills for detailed evidence and revision protocols.
Settle task subtasks with `aq task subtask-done N` or
`aq task subtask-skip N --note ...`; do not bypass required open children or human gates.

## Prepare feature history before review

Before the first passing feature close, squash implementation commits on your own leaf
branch to one commit based on its recorded source base; preserve tree, attribution and
checks. Never rewrite reviewed/delivered history, another worker's branch, or parent merge
ancestry. Save the successful push's full OID. After a local squash of already-pushed work,
use `aq git push --expected-remote-oid <pushed-oid>`; a moved remote requires escalation,
never a guessed lease or unconditional force push. All-zero OID is only for an absent branch.
Review binds the final pushed SHA; later fixes require fresh review.

## Never close over unpushed commits

Commit and publish your assigned task branch with `aq git push` before pass or fail close;
record HEAD and checks. Slots reset after release. A failed close preserves unpushed work
on an AQ recovery branch; if its push fails, the task remains held. Fix publication before
retrying. For a required PR use `aq git create-pr --title "..." --body "..."` after pushing.
The daemon supplies credentials. Do not bypass capability denials; report them.
Operator-only repair (`aq doctor --check profiles.system_drift` and
`aq agent profile-reseed --profile-id <id> --grants-only`) is out of scope for workers.

## Stacked branches: avoid them

Stay on your assigned branch. Stack only for a genuine prerequisite; declare it in the
close summary. Delivery belongs to the configured integration owner
(docs/concepts/factory-policy.md); never merge or push the default branch yourself.
A PR into a feature branch has not delivered to the default branch.

## Stay visible while you work

Before minutes of quiet foreground work, run `aq task heartbeat {task_id}`; renew during
long work. For supported long work, register one durable wait and end the turn instead
(managed tests: `aq test --aq-detach --aq-wait --aq-idempotency-key KEY TEST_ARGS` when job
admission is enabled). An active durable wait holds the lease without heartbeats; resume
with `aq wait show WAIT_ID --consume --json` and handle its actual outcome or timeout.
Never manage the operator daemon.

## Save findings before closing or handing off

Record material findings/decisions, exact checks and evidence paths while working with
`aq task comment {task_id} --body "Finding: ... Evidence: ... Next: ..."`.
For changed requirements read the full description first and update with
`aq task set {task_id} --description "..." --expected-description "<read value>"`;
preserve the goal/acceptance criteria and merge conflicts by rereading. No secrets.
Comments are durable evidence, not approval or notification. For a blocking human decision,
use `aq message send --to user:dashboard --project "$AQ_PROJECT_ID" --body "Blocked: ..."`.
