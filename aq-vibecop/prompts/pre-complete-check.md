# Pre-Completion Quality Check

Before marking your task as complete, run vibecop to catch antipatterns in your changes.

## Checklist

- [ ] Have you run `vibecop_scan` with `diff_ref` set to the base branch to check only your changes?
- [ ] Are there any **error-severity** findings? If so, fix them before completing the task.
- [ ] Have you re-run `vibecop_scan` after fixing to confirm a clean scan (no remaining errors)?

## Workflow

1. **Scan your changes:**
   ```
   vibecop_scan(diff_ref="main")
   ```
   Set `diff_ref` to the base branch your task branched from (usually `main`). This scans only files you changed, not the entire project.

2. **Review findings:**
   - **Errors** — Must fix. These indicate bugs, security vulnerabilities, or correctness issues.
   - **Warnings** — Should fix. These are code quality antipatterns that make maintenance harder.
   - **Info** — Optional. Consider fixing but not required for task completion.

3. **Fix error-severity findings:**
   Apply the suggested fixes from the scan results. Common fixes include:
   - Breaking up god-functions into smaller, focused functions
   - Parameterizing SQL queries to prevent injection
   - Adding null/error checks for unchecked database results
   - Removing dead code paths and unused imports

4. **Verify fixes:**
   ```
   vibecop_check(files=["path/to/fixed_file.py", "path/to/other_file.ts"])
   ```
   Or re-run the full diff scan:
   ```
   vibecop_scan(diff_ref="main")
   ```

5. **Complete the task** once all error-severity findings are resolved.

## When to Skip

- If `vibecop_status` reports vibecop is not installed, note this in your completion summary and proceed.
- If the scan times out on a very large changeset, run `vibecop_check` on the most critical files instead.
- Tasks of type `docs`, `chore`, `research`, `plan`, or `sync` do not require vibecop scanning.
