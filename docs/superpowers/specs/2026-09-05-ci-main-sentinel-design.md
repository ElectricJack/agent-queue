# CI main sentinel — keep `main` green without a human in the loop

## Problem

`main` goes red sometimes; that is not preventable. What was preventable on
2026-09-05 was what happened next: two workers on `brisk-beacon.10` each
diagnosed a red PR check as a broken base, had no sanctioned way to act on
that, and closed the task as a hard failure. Nothing filed a fix for `main`,
nothing told anyone, and the task sat terminally blocked until an operator
asked. The RCA is in the task's comment history; the policy gap it exposed is
that the fleet had no owner for the health of `main` itself.

## Decision

A project-scoped V2 playbook, `ci-main-sentinel`, owns `main`'s health. It is
shaped exactly like the shipped `pr-merge-sweep`: a timer trigger, one rule,
deterministic command steps, no LLM step in the playbook itself. The only
intelligence is in the repair task it files.

- **Trigger:** `timer.15m`, scope `project:agent-queue`.
- **Step 1 — observe.** Call the new read-only command `ci_baseline_status`
  for the project's default branch. It reads the head commit's check runs
  through `gh`, judges them with the same `classify_rollup` the merge gate
  uses, pulls the failing test ids out of the failed jobs' logs, and derives a
  **failure signature** (a digest of the sorted failing test ids, falling
  back to the failing check names when logs are unreadable).
- **Step 2 — repair.** On `red`, `ensure_task` a repair task keyed
  `ci-baseline:<signature>:<attempt>` at `intelligence_class` `deep-high`,
  priority `5`. The command pre-renders the title and description (head sha,
  failing checks and tests, run URL, and the rule "smallest change that makes
  `main` green, through a PR, no feature work"), so the playbook binds them
  and carries no prose of its own.
- **Step 3 — escalate.** On `red_escalated` — the same signature has already
  had two attempts that completed or blocked and `main` is still red — open a
  `human` gate keyed `ci-baseline-escalation:<signature>` instead of a third
  task. A fixer that cannot fix `main` is an incident, not a retry.
- `green`, `pending` and `unknown` end the run; the next tick looks again.

## Why the signature, not the commit

A new commit that leaves the same tests red is the same problem and must not
spawn a second fixer while the first is in flight. A different set of failing
tests is a different problem and deserves its own task. Keying on the commit
would do the opposite of both.

## Why attempts are counted by the command

`ensure_task` reuses any non-terminal task with the key, and a hard-failed
repair task is BLOCKED, which is non-terminal. Keying every attempt separately
and letting the command decide whether a new key is warranted keeps the
playbook free of state and keeps the escalation rule in tested Python rather
than in prose.

## Attribution for other playbooks

The command's result is also the answer to "was this test already red on
`main`?". A branch whose failing checks match the sentinel's current
signature is not at fault; that comparison is the follow-up that lets the
reviewer park a branch on a `ci_baseline` gate instead of rejecting it. This
spec ships the observation and the repair loop; the parking rule is a later,
separate change so this one stays small.

## Files

- `src/commands/ci_commands.py` — `ci_baseline_status`.
- `src/git/manager.py` — `acommit_head_sha`, `acommit_check_runs`,
  `ajob_failed_tests` (all `gh`-backed, `None` on any failure to read).
- `src/database/queries/task_queries.py` — `list_tasks_by_dedup_prefix`.
- `src/commands/contracts/builtin.py` — the contract: outcomes `green`,
  `red`, `red_escalated`, `pending`, `unknown`, `rejected`.
- `src/prompts/project_playbooks/agent-queue/ci-main-sentinel.md` and its
  reviewed bundle under `tests/fixtures/playbooks/v2/ci-main-sentinel/`.

## Activation

Like the sweep, the bundle is imported and activated per install:

    aq playbook v2-import --path reviewed-playbooks/ci-main-sentinel
    aq playbook activate --playbook-id ci-main-sentinel --artifact-sha256 <hash>

The daemon must be running code that ships `ci_baseline_status`; import
validates every command the artifact names against the live registry and
refuses otherwise.
