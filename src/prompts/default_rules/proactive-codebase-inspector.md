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
1. List files in the project workspace using `git ls-files` (respects .gitignore).
   Exclude binary files, images, fonts, lockfiles, `__pycache__/`, `node_modules/`,
   and generated output directories.
2. Categorize each file and randomly select one using weighted categories:
   - Source code (40%): `*.py`, `*.ts`, `*.js`, etc.
   - Specs/docs (20%): `specs/*.md`, `docs/*.md`, `README.md`
   - Tests (15%): `tests/**`, `test/**`
   - Configuration (10%): `pyproject.toml`, `Dockerfile`, CI configs
   - Recently modified within 7 days (15%)
3. If the file is large (>300 lines), select a random contiguous section of ~150 lines.
   For smaller files, read the entire file.
4. Check inspection history in project memory — if this file was inspected in the
   last 3 cycles, re-roll (up to 3 retries, then accept).
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
9. Record the inspected file path and timestamp in project memory to ensure broad
   coverage over time and avoid re-inspecting the same files repeatedly.
