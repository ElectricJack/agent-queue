# Test Suite Results & Fix Plan

<!-- aq:historical -->
> **Historical working note.** Dated scratch material, kept for the record and
> never revised. Not instructions. Start at [the documentation
> home](../docs/README.md); see [historical
> material](../docs/history/README.md).

**Date:** 2026-03-12
**Branch:** clear-impact/run-test-suite-and-generate-fix-plan
**Overall:** 2 failed, 1,680 passed, 393 skipped, 4 warnings in 268.74s (4m 28s)

---

## Failed Tests

### Failure 1: `tests/chat_eval/test_tool_coverage.py::test_all_tools_have_test_cases`

**Error Message:**
```
AssertionError: 1 tool(s) have no test cases:
  - set_default_branch
assert not {'set_default_branch'}
```

**Root Cause:**
The `set_default_branch` tool was added to the TOOLS list in `src/chat_agent.py` (lines 146-160)
and implemented in `src/command_handler.py` (`_cmd_set_default_branch()`, lines 992-1056),
but no corresponding chat-eval test cases were created to cover it.

The coverage check in `test_tool_coverage.py` ensures every tool in TOOLS has at least one
test case; `set_default_branch` is the only tool missing coverage.

**Fix:**
Add test cases for `set_default_branch` to `tests/chat_eval/test_cases/projects.py`
(or a new file). The tool takes `project_id` (str) and `branch` (str) parameters.

Example test cases to add:

```python
# In tests/chat_eval/test_cases/projects.py — append to CASES list:

TestCase(
    id="proj-set-default-branch",
    description="Explicit set default branch command",
    category="projects",
    difficulty=Difficulty.EASY,
    tags=["set_default_branch", "write"],
    turns=[
        Turn(
            user_message="set the default branch of project my-app to develop",
            expected_tools=[
                ExpectedTool(name="set_default_branch", args={"project_id": "my-app", "branch": "develop"}),
            ],
        ),
    ],
),
TestCase(
    id="proj-set-default-branch-natural",
    description="Natural language request to change default branch",
    category="projects",
    difficulty=Difficulty.MEDIUM,
    tags=["set_default_branch", "write", "natural-language"],
    turns=[
        Turn(
            user_message="change the default branch for my-app to main",
            expected_tools=[
                ExpectedTool(name="set_default_branch", args={"project_id": "my-app", "branch": "main"}),
            ],
        ),
    ],
),
```

---

### Failure 2: `tests/test_git_manager.py::TestCreateGithubRepo::test_uses_yes_flag`

**Error Message:**
```
AssertionError: assert '--yes' in ['gh', 'repo', 'create', 'my-app', '--private']
```

**Root Cause:**
Commit `94671b7` ("Fix gh repo create using invalid --yes flag") intentionally removed the
`--yes` flag from `GitManager.create_github_repo()` in `src/git/manager.py` because
`--yes` is not a valid flag for `gh repo create` (it only exists on `gh repo delete`).
The fix was correct, but the test at `tests/test_git_manager.py:2743` was not updated
to match the new behavior.

**File:** `tests/test_git_manager.py`, line 2743-2758

**Fix:**
Update the test to verify `--yes` is **not** present (or remove/rewrite the test entirely).
The test should validate the corrected behavior:

```python
# tests/test_git_manager.py, line 2743 — Replace the existing test:

def test_uses_yes_flag(self, monkeypatch):
    """--yes flag is NOT used (it's invalid for gh repo create)."""
    mgr = GitManager()
    captured_args = {}

    def mock_run(cmd, **kwargs):
        captured_args["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="https://github.com/user/my-app\n", stderr="",
        )

    monkeypatch.setattr(subprocess, "run", mock_run)

    mgr.create_github_repo("my-app")
    assert "--yes" not in captured_args["cmd"]
    assert "--confirm" not in captured_args["cmd"]
```

Alternatively, rename the test to `test_no_yes_or_confirm_flag` to better reflect intent.

---

## Warnings (non-blocking)

| Warning | Location | Notes |
|---------|----------|-------|
| `audioop` deprecation | `discord/player.py:30` | Upstream `discord.py` issue; will be fixed when they release Python 3.13 support |
| Unknown `pytest.mark.eval` | `tests/chat_eval/test_eval_integration.py:26` | Register the mark in `pyproject.toml` `[tool.pytest.ini_options].markers` |
| `TestCaseResult` collection warning | `tests/chat_eval/metrics.py:67` | Rename class or add `__test__ = False` |
| `TestCase` collection warning | `tests/chat_eval/test_cases/_types.py:37` | Rename class or add `__test__ = False` |

---

## Summary of Required Changes

| # | File | Change | Impact |
|---|------|--------|--------|
| 1 | `tests/chat_eval/test_cases/projects.py` | Add 2+ test cases for `set_default_branch` tool | Fixes `test_all_tools_have_test_cases` |
| 2 | `tests/test_git_manager.py:2743-2758` | Update `test_uses_yes_flag` to assert `--yes` is **absent** | Fixes `test_uses_yes_flag` |

Both fixes are test-only changes with no production code modifications needed.
