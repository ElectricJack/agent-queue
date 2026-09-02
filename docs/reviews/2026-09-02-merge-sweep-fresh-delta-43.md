# Merge sweep: fresh-delta-43

**Sweep snapshot:** 2026-09-02T10:12:08Z

**Task branch:** `aq/fresh-delta-43`

**Repository:** `ElectricJack/agent-queue`

## Outcome

The sweep merged pull requests #184 through #195. Nine merged cleanly and were landed
immediately without tests, as required by the merger profile. Three had conflicts; each was
updated with `origin/main`, resolved while retaining both branches' intent, verified with
targeted tests, pushed, and then merged.

## Clean merges

- #185 — `fix(tests): restore ruff cleanliness on test_cli_daemon; harden the aq logs
  regression test`
- #186 — `fix(aq test): refuse nonexistent test paths instead of passing green`
- #187 — `docs: review verdict for azure-meadow / PR #108`
- #188 — `Fix aq task progress crash: give task_progress a formatter proxy`
- #189 — `fix(pipeline): derive review_task at dispatch so a review is never reviewed`
- #190 — `docs(skills): correct every aq invocation against the real CLI, and guard it`
- #191 — `Playbook V2 pkg3 artifact store + activation`
- #193 — `fix(api): render the daemon-fetched OpenAPI spec the way the drift guard requires`
- #195 — `Give explain/gate/session commands real CLI and MCP schemas`

## Conflict-resolved merges

- #184 — retained `src.claim_file` imports from `main` and the pull request's public reconciler
  constant bindings. Targeted verification: 504 passed, 2 skipped.
- #192 — combined the derived `review_task` recursion guard with the empty-branch `no_code`
  guard. Targeted verification: 266 passed.
- #194 — retained mutable artifact refresh together with the dialect-safe activation upsert
  and fallback, including its regression coverage. Verification: Ruff clean; 1,544 passed,
  26 skipped.

## Skipped at the snapshot boundary

- #196 and #197 were updated less than five minutes before the candidate snapshot.
- #198 and later did not exist in the candidate set when the sweep began.

This record is intentionally limited to that single sweep. Later pull requests belong to a
subsequent sweep rather than extending this task indefinitely.
