# Proactive Codebase Inspector

## Intent
Periodically inspect random sections of the project's source code, documentation,
specs, tests, and configuration to identify potential improvements, issues, or risks
that the team hasn't explicitly flagged. This is the system's only mechanism that
reads and analyzes source artifacts directly on a recurring basis without being
triggered by a specific event, task, or conversation.

## Trigger
Check every 4 hours.

## Logic
1. Call the `select_files_for_inspection` tool (files plugin) with the current
   `project_id` and a modest `count` (1–3 files per cycle). The tool:
   - Enumerates tracked files via `git ls-files` (respects .gitignore) with a
     filesystem-walk fallback for non-git workspaces.
   - Excludes binary files, images, fonts, generic lockfiles, `__pycache__/`,
     `node_modules/`, and generated output directories.
   - Categorizes each file (`source`, `specs`, `tests`, `config`, `recent`) and
     samples using the weighted distribution:
     source 40%, specs 20%, tests 15%, config 10%, recent-changes 15%.
   - Reads `inspections` namespace from project memory and de-prioritizes files
     inspected in the last `history_lookback_days` (default 21).
2. If the returned `files` list is empty, log completion with no inspection and
   exit this cycle.
3. For each selected file, if the file is large (>300 lines), select a random
   contiguous section of ~150 lines. For smaller files, read the entire file.
4. The history exclusion is handled by the tool in step 1; no manual re-roll
   is required. If a second pass is needed (e.g. only recent files in 3 cycles),
   raise `history_lookback_days` or pass `weights` overrides.
5. Read the selected content and analyze it for:
   - Code quality issues (complexity, dead code, unclear naming, duplication)
   - Performance concerns (blocking calls in async code, missing caching, N+1 patterns)
   - Security risks (hardcoded secrets, injection vectors, missing input validation)
   - Error handling gaps (bare except clauses, swallowed errors, missing error paths)
   - Documentation accuracy and completeness vs actual code behavior
   - Test coverage gaps and test quality issues
   - Architectural concerns (tight coupling, circular deps, missing abstractions)
   - Stale TODO/FIXME/HACK comments that have gone unaddressed
6. Decide if any finding is significant enough to suggest to the team:
   - SKIP trivial style nits that don't affect functionality
   - SKIP things that are clearly intentional design choices
   - SKIP if uncertain — when broader context would be needed to judge
   - SUGGEST only concrete, actionable findings worth the team's attention
7. If a finding is worth suggesting: post a suggestion with the file path,
   specific finding, severity (low/medium/high), and a recommended action.
8. If nothing notable: log that the inspection completed with no actionable findings.
9. Record the inspection by calling the `record_file_inspection` tool with the
   `file_path`, an optional short `summary`, and `findings_count`. This writes
   the inspection to the `inspections` namespace in project memory so that
   `select_files_for_inspection` on the next cycle can avoid re-inspecting
   the same files.
