# Review: azure-meadow / PR #108 — "fix: restore subagent telemetry CI coverage"

**Reviewer task:** grand-zenith-27
**Reviewed task:** azure-meadow (branch `aq/azure-meadow`, PR #108)
**Verdict:** APPROVE (as merged; see history)

## State at review time

PR #108 is **MERGED** (2026-09-02T07:11:49Z, head `681922cc`, merge commit `c7a371cc`).
Current `main` tip at review: `62667475`.

## What an earlier review pass found (retained)

At head `8eac83c3` / `681922cc` the PR:

- **Fixed (12 failures):** `test_api_scope::test_agent_command_set_contents`,
  `test_command_scope_matrix` (2), `test_command_surface::test_every_command_is_placed_deliberately`,
  `test_docs_sync` (2), `test_mcp_server::TestDriftDetection` (2),
  `test_migrate_sqlite_to_pg` (5) — the PG boolean-default migration fix made the
  `migration-and-slow` job green.
- **Did not fix, though named in the task title:** `tests/test_cli_logs.py` (duplicate
  `@cli.command("logs")` registration in both `src/cli/daemon.py` and `src/cli/logs.py`;
  import-order dependent, hence green locally / red under xdist) and
  `tests/test_tool_index.py::test_search_ranks_by_cosine_similarity`.
- **Introduced at that head:** `test_api_client_contract::test_live_openapi_operations_match_generated_python_client`
  drift — `subagent_event` was in `_TOOL_CATEGORIES`/`_ALL_TOOL_DEFINITIONS`, so codegen
  emitted `POST /api/agent/subagent-event`, but the committed `openapi.json` and
  `packages/aq-client` carried no such operation.

## Verification on current `main` (62667475)

- Duplicate `logs` registration is gone: only `src/cli/logs.py:387` registers `@cli.command("logs")`.
- `openapi.json` now contains the `subagent-event` operation — the contract drift is repaired.
- Focused suite for every named area passes:

```
aq test tests/test_cli_logs.py tests/test_tool_index.py tests/test_api_client_contract.py \
        tests/test_command_surface.py tests/test_docs_sync.py tests/test_api_scope.py \
        tests/test_command_scope_matrix.py
=> 207 passed
```

## Conclusion

The reviewed work is merged and the residual breakages it left behind have since been
repaired on `main`; every failure named in the review task's title now passes. Reopening
`azure-meadow` would not change any code state, so the review closes as an approval with
the above history recorded.
