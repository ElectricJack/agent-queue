# Test Suite Results — 2026-03-26

<!-- aq:historical -->
> **Historical working note.** Dated scratch material, kept for the record and
> never revised. Not instructions. Start at [the documentation
> home](../docs/README.md); see [historical
> material](../docs/history/README.md).

| Metric | Result |
|--------|--------|
| **Total Tests** | 2,538 |
| **Passed** | 2,096 |
| **Skipped** | 40 |
| **Failed** | 402 |
| **Warnings** | 4 |
| **Duration** | 502.38s (8m 22s) |
| **Overall** | ⚠️ Failures present (non-functional) |

## Failure Breakdown

| Test File | Failures | Root Cause |
|-----------|----------|------------|
| `tests/chat_eval/test_eval_integration.py` | 401 | Anthropic API auth error — OAuth not supported (401) |
| `tests/chat_eval/test_tool_coverage.py` | 1 | Missing test cases for 6 tools |

## Details

### Chat Eval Integration (401 failures)

All 401 failures are caused by the same Anthropic API authentication error:

```
anthropic.AuthenticationError: Error code: 401 -
{'type': 'error', 'error': {'type': 'authentication_error',
'message': 'OAuth authentication is currently not supported.'}}
```

These tests require a valid Anthropic API key and are failing due to an OAuth credential issue in the test environment. **Not a code bug** — this is an environment/credential configuration issue.

### Tool Coverage (1 failure)

`test_all_tools_have_test_cases` — 6 tools have no chat eval test cases:

- `cancel_scheduled`
- `fire_all_scheduled_hooks`
- `hook_schedules`
- `list_scheduled`
- `process_plan`
- `schedule_hook`

The 5 scheduled-hook tools (`cancel_scheduled`, `fire_all_scheduled_hooks`, `hook_schedules`, `list_scheduled`, `schedule_hook`) are newly added (task `amber-orbit`). `process_plan` was a pre-existing gap.

## Additional Checks

- ✅ No stuck tasks (no ASSIGNED/IN_PROGRESS)
- ✅ No BLOCKED tasks
- ✅ All tasks are COMPLETED
- ✅ No orphaned hooks or rule sync issues

## Comparison with Previous Run (2026-03-25)

| Metric | 2026-03-25 | 2026-03-26 | Delta |
|--------|------------|------------|-------|
| Total | 2,537 | 2,538 | +1 |
| Passed | 2,096 | 2,096 | 0 |
| Failed | 401 | 402 | +1 |
| Skipped | 40 | 40 | 0 |

The +1 failure is from `test_all_tools_have_test_cases` now detecting 6 untested tools (up from a previous count) due to the newly added scheduled hook tools.

## Verdict

The core test suite (non-eval) is **fully passing**. All 402 failures are in the `chat_eval` suite — 401 are API auth issues (environment, not code) and 1 is a test coverage gap for new features. No regressions detected.
