# Test Suite Results & Fix Plan

<!-- aq:historical -->
> **Historical working note.** Dated scratch material, kept for the record and
> never revised. Not instructions. Start at [the documentation
> home](../docs/README.md); see [historical
> material](../docs/history/README.md).

**Date:** 2026-03-14
**Branch:** agile-rapids/run-test-suite-and-create-fix-plan
**Test Run Summary:** 2 failed, 1,727 passed, 393 skipped, 4 warnings (~4m 49s)

---

## Failure Details

### Failure 1: `test_all_tools_have_test_cases` (tests/chat_eval/test_tool_coverage.py)

**What:** The `remove_workspace` and `set_default_branch` tools were added to the TOOLS list but have no corresponding chat-eval test cases.

**Root Cause:** When these two tools were implemented, no one added the required chat-eval test cases. The coverage test enforces that every registered tool has at least one test case.

**Severity:** Medium — does not affect runtime behavior, but blocks CI.

### Failure 2: `test_arun_timeout` (tests/test_git_manager_async.py)

**What:** When `_arun` times out and the subprocess has already exited by the time `proc.kill()` is called, a `ProcessLookupError` is raised instead of the expected `GitError`.

**Root Cause:** Race condition in `src/git/manager.py:_arun` (line 134). After `asyncio.TimeoutError` is caught, the code calls `proc.kill()` unconditionally. If the process already exited, `proc.kill()` raises `ProcessLookupError` which is not caught, preventing the `GitError` from being raised.

**Severity:** High — this is a runtime bug. Any git command that times out but whose process exits before `kill()` runs will crash with an unhandled `ProcessLookupError` instead of a clean `GitError`.

---

## Phase 1: Fix `_arun` timeout race condition (HIGH priority)

**File:** `src/git/manager.py`, lines 133-135

Wrap `proc.kill()` in a try/except to handle `ProcessLookupError`:

```python
except asyncio.TimeoutError:
    try:
        proc.kill()
    except ProcessLookupError:
        pass  # process already exited
    await proc.wait()
    raise GitError(
        f"git {' '.join(args)} timed out after "
        f"{effective_timeout}s (possible credential prompt)"
    )
```

**Verification:** Run `pytest tests/test_git_manager_async.py::TestArun::test_arun_timeout -v`

## Phase 2: Add chat-eval test cases for `remove_workspace` and `set_default_branch` (MEDIUM priority)

**Files:**
- `tests/chat_eval/test_cases/workspaces.py` — add 2+ test cases for `remove_workspace`
- `tests/chat_eval/test_cases/projects.py` — add 2+ test cases for `set_default_branch`

Look at existing test cases in those files for the pattern (each case is a `TestCase` dataclass with a user message, expected tool name, and expected arguments). Add realistic user prompts that should trigger each tool.

**Verification:** Run `pytest tests/chat_eval/test_tool_coverage.py::test_all_tools_have_test_cases -v`
