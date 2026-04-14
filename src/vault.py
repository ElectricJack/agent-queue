"""Vault directory structure initialization and migration.

Creates the ``~/.agent-queue/vault/`` directory tree described in
``docs/specs/design/vault.md`` §2.  The vault is a structured, human-readable
knowledge base (Obsidian-compatible) that serves as the single source of truth
for system configuration and accumulated intelligence.

The top-level structure is created once at orchestrator startup via
``ensure_vault_structure()``.  Per-profile and per-project subdirectories are
created dynamically as profiles and projects are added.

All directory creation is idempotent — calling any function when the directories
already exist is a safe no-op.

The consolidated migration entry point ``run_vault_migration()`` (spec §6)
orchestrates all Phase 1 migrations in the correct order, supports dry-run
mode, and returns a detailed report.
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def migrate_notes_to_vault(data_dir: str, project_id: str) -> bool:
    """Move project notes from ``notes/{project_id}/`` to ``vault/projects/{project_id}/notes/``.

    Part of vault migration Phase 1 (spec §6).  Moves all files (preserving
    any subdirectory structure) from the legacy ``notes/{project_id}/``
    directory into the vault's per-project notes directory.

    The operation is **idempotent**:

    * If the source directory does not exist, nothing happens (returns ``False``).
    * If a destination file already exists, that individual file is skipped.
    * After all files are moved, empty source directories are removed.
    * Calling the function again after a successful migration is a safe no-op.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        project_id: The project identifier (e.g. ``mech-fighters``).

    Returns:
        ``True`` if any files were moved, ``False`` if skipped entirely.
    """
    source = os.path.join(data_dir, "notes", project_id)
    dest = os.path.join(data_dir, "vault", "projects", project_id, "notes")

    if not os.path.isdir(source):
        logger.debug(
            "Notes migration for %s: source %s does not exist, skipping",
            project_id,
            source,
        )
        return False

    # Ensure the destination exists before moving files into it.
    os.makedirs(dest, exist_ok=True)

    moved_any = False
    for dirpath, _dirnames, filenames in os.walk(source):
        # Compute relative path from source root
        rel_dir = os.path.relpath(dirpath, source)
        dest_dir = os.path.join(dest, rel_dir) if rel_dir != "." else dest
        os.makedirs(dest_dir, exist_ok=True)

        for fname in filenames:
            src_file = os.path.join(dirpath, fname)
            dst_file = os.path.join(dest_dir, fname)

            if os.path.exists(dst_file):
                logger.debug(
                    "Notes migration for %s: %s already exists at destination, skipping",
                    project_id,
                    fname,
                )
                continue

            shutil.move(src_file, dst_file)
            moved_any = True
            logger.debug("Moved note %s → %s", src_file, dst_file)

    # Clean up empty source directories (bottom-up).
    for dirpath, dirnames, filenames in os.walk(source, topdown=False):
        if not filenames and not dirnames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass  # Not empty or permission issue — leave it

    # Remove the top-level source dir if it's now empty.
    try:
        os.rmdir(source)
    except OSError:
        pass

    if moved_any:
        logger.info(
            "Migrated notes for project %s from %s to %s",
            project_id,
            source,
            dest,
        )
    return moved_any


def migrate_rule_files(data_dir: str) -> dict:
    """Move rule markdown files from ``memory/`` to vault playbook locations.

    Part of vault migration Phase 1 (spec §6).  Moves rule files:

    - **Global rules:** ``memory/global/rules/*.md`` →
      ``vault/system/playbooks/``
    - **Project rules:** ``memory/{project_id}/rules/*.md`` →
      ``vault/projects/{project_id}/playbooks/``

    The operation is **idempotent**:

    * Files already present at the destination are skipped.
    * Source files are removed only after a successful copy.
    * Each move is logged individually for auditability.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        Dict with ``moved``, ``skipped``, and ``errors`` counts plus a
        ``details`` list of per-file log messages.
    """
    stats: dict = {"moved": 0, "skipped": 0, "errors": 0, "details": []}
    memory_root = os.path.join(data_dir, "memory")

    if not os.path.isdir(memory_root):
        logger.debug("Rule file migration: memory root %s does not exist, skipping", memory_root)
        return stats

    for scope_dir in sorted(os.listdir(memory_root)):
        rules_dir = os.path.join(memory_root, scope_dir, "rules")
        if not os.path.isdir(rules_dir):
            continue

        # Determine vault destination based on scope
        if scope_dir == "global":
            dest_dir = os.path.join(data_dir, "vault", "system", "playbooks")
        else:
            dest_dir = os.path.join(data_dir, "vault", "projects", scope_dir, "playbooks")

        # Ensure destination directory exists
        os.makedirs(dest_dir, exist_ok=True)

        for filename in sorted(os.listdir(rules_dir)):
            if not filename.endswith(".md"):
                continue

            src_path = os.path.join(rules_dir, filename)
            dest_path = os.path.join(dest_dir, filename)

            # Idempotent: skip if destination already exists
            if os.path.exists(dest_path):
                stats["skipped"] += 1
                detail = f"SKIP {scope_dir}/{filename}: already at destination"
                stats["details"].append(detail)
                logger.debug("Rule migration: %s already at %s, skipping", filename, dest_dir)
                continue

            try:
                shutil.copy2(src_path, dest_path)
                os.remove(src_path)
                stats["moved"] += 1
                detail = f"MOVE {scope_dir}/{filename}: {rules_dir} → {dest_dir}"
                stats["details"].append(detail)
                logger.info("Migrated rule file %s → %s", src_path, dest_path)
            except Exception as e:
                stats["errors"] += 1
                detail = f"ERROR {scope_dir}/{filename}: {e}"
                stats["details"].append(detail)
                logger.warning("Failed to migrate rule file %s: %s", src_path, e)

    if stats["moved"] or stats["errors"]:
        logger.info(
            "Rule file migration complete: %d moved, %d skipped, %d errors",
            stats["moved"],
            stats["skipped"],
            stats["errors"],
        )

    return stats


def migrate_obsidian_config(data_dir: str) -> bool:
    """Move Obsidian config from ``memory/.obsidian/`` to ``vault/.obsidian/``.

    Part of vault migration Phase 1 (spec §6).  Moves the entire
    ``.obsidian/`` directory — themes, plugins, workspace layout — from the
    legacy ``memory/`` location to the new ``vault/`` root.

    The operation is **idempotent**:

    * If the source does not exist, nothing happens (returns ``False``).
    * If the destination already exists, nothing happens (returns ``False``).
    * Only when the source exists *and* the destination does not will the
      move be performed (returns ``True``).

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        ``True`` if the move was performed, ``False`` if skipped.
    """
    source = os.path.join(data_dir, "memory", ".obsidian")
    dest = os.path.join(data_dir, "vault", ".obsidian")

    if not os.path.isdir(source):
        logger.debug("Obsidian config migration: source %s does not exist, skipping", source)
        return False

    if os.path.exists(dest):
        logger.debug("Obsidian config migration: destination %s already exists, skipping", dest)
        return False

    # Ensure the vault/ parent directory exists before moving into it.
    os.makedirs(os.path.join(data_dir, "vault"), exist_ok=True)

    shutil.move(source, dest)
    logger.info("Migrated Obsidian config from %s to %s", source, dest)
    return True


# ---------------------------------------------------------------------------
# Default template content (vault spec §2, profiles spec §2 & §4)
# ---------------------------------------------------------------------------

PROFILE_TEMPLATE = """\
---
id: my-agent
name: My Agent
tags: [profile, agent-type]
---

# My Agent

## Role
You are a software engineering agent. Describe your agent's role, expertise,
and behavioral expectations here. This section is injected into the agent's
system prompt as-is.

## Config
```json
{
  "model": "claude-sonnet-4-6",
  "permission_mode": "auto"
}
```

## Tools
```json
{
  "allowed": [],
  "denied": []
}
```

## MCP Servers
```json
{}
```

## Rules
- List behavioral rules for this agent type
- These are injected into the agent's system prompt
- Example: Always run existing tests before committing
- Example: Never commit secrets, .env files, or credentials

## Reflection
After completing a task, consider:
- Did I encounter any surprising behavior worth remembering?
- Did I resolve an error that might recur? If so, save the pattern.
- Is there a convention in this project I should note for next time?

## Install
```json
{
  "npm": [],
  "pip": [],
  "commands": []
}
```
"""

PLAYBOOK_TEMPLATE = """\
---
id: my-playbook
triggers:
  - task.completed
scope: system
enabled: true
---

# My Playbook

Describe what this playbook does in plain English. Playbooks are directed
graphs of LLM decision points — each step is a focused prompt, and the
LLM decides which path to take based on accumulated context.

Write your process as you would explain it to a colleague. The system
compiles this natural language into an executable workflow graph.

## Example Flow

On receiving the trigger event, first analyze the event data to understand
what happened.

If the analysis reveals issues that need attention, create follow-up tasks
with appropriate priority.

If everything looks good, log the outcome to project memory and finish.
"""

# Starter knowledge packs (profiles spec §4)
_STARTER_KNOWLEDGE: dict[str, dict[str, str]] = {
    "coding": {
        "common-pitfalls.md": """\
---
tags: [starter, coding, pitfalls]
---

# Common Pitfalls

Known patterns that cause problems in software engineering tasks.
This file is seeded from a starter template — update it as you
accumulate real experience.

## Async / Sync Mismatches
- Never use synchronous I/O (e.g. `subprocess.run()`, `open().read()`)
  in async code paths — it blocks the event loop and stalls all
  concurrent tasks
- When calling async APIs from sync contexts, use an event loop bridge
  (not `asyncio.run()` inside an already-running loop — that raises
  `RuntimeError`)
- Watch for libraries that look async but do sync I/O internally
  (e.g. some ORM operations, DNS resolution)
- `asyncio.to_thread()` is the correct way to run blocking code from
  async contexts (Python 3.9+)

## Import Cycles
- Circular imports often manifest as `AttributeError` at runtime, not
  at import time — the partially-initialised module is missing the
  symbol you need
- Use local imports inside functions to break cycles when needed
- Prefer moving shared types to a separate module (e.g. `types.py`,
  `models.py`) that both sides can import without circularity
- `TYPE_CHECKING` guards (`from __future__ import annotations` +
  `if TYPE_CHECKING:`) let you use types for hints without runtime
  import

## Silent Failures
- Bare `except: pass` swallows errors that could help debug later
  issues — always catch specific exception types
- Always log caught exceptions with `logger.exception()` or
  `logger.error(..., exc_info=True)` — even if you choose not to
  re-raise
- Watch for functions that return `None` on failure instead of raising
  — callers may not check the return value
- Async tasks that raise exceptions silently disappear if you don't
  `await` or attach a done callback

## Type Mismatches
- JSON `null` becomes Python `None` — check before accessing attributes
  or calling methods on parsed data
- Dict `.get()` returns `None` by default, which may not be falsy
  enough (e.g. `0` and `""` are falsy but valid values — use a
  sentinel: `d.get(key, _MISSING)`)
- String IDs vs integer IDs: databases may return `int`, while JSON
  APIs return `str` — normalise early and consistently
- `bool` is a subclass of `int` in Python: `isinstance(True, int)`
  is `True`, so order your type checks carefully

## Resource Management
- Always use context managers (`with` / `async with`) for files,
  database connections, HTTP sessions, and locks
- Forgetting to close an HTTP client leaks connections and may exhaust
  the OS file-descriptor limit under load
- Temporary files and directories need cleanup — use `tempfile` context
  managers rather than manual creation + deletion
- Database connections returned to a pool in an error state can poison
  the next caller — ensure proper rollback on failure

## Error Handling Anti-Patterns
- Catching `Exception` too broadly hides bugs — catch the narrowest
  type that makes sense (e.g. `ValueError`, `KeyError`, `httpx.HTTPError`)
- Re-raising with bare `raise` preserves the original traceback;
  `raise new_exception from original` chains them properly
- Don't use exceptions for control flow in performance-sensitive paths
  — check conditions first (LBYL) when feasible
- Retries without backoff or a maximum count can cause infinite loops
  or thundering-herd effects

## Concurrency and Shared State
- Shared mutable state between async tasks causes race conditions even
  without threads — any `await` is a potential context switch
- Prefer message-passing (queues) or immutable data over shared dicts
  and lists
- `asyncio.Lock` is not thread-safe; `threading.Lock` is not
  async-safe — use the right one for your context
- When spawning background tasks, always store a reference and handle
  cancellation; orphaned tasks leak memory

## Configuration and Environment
- Never hard-code file paths, URLs, or credentials — load from config
  or environment variables
- Missing environment variables should fail loudly at startup, not at
  first use ten minutes later
- Default values for config should be safe (e.g. default to read-only,
  default to localhost, default to stricter limits)
- Validate configuration at load time with clear error messages; don't
  let bad config propagate deep into the system

## Dependency Gotchas
- Pinned versions prevent unexpected breakage; unpinned versions
  invite it — but over-pinning creates upgrade debt
- Check that optional dependencies are actually installed before
  importing them — use try/except ImportError with clear messaging
- Watch for breaking changes in minor versions of pre-1.0 packages
  (semver says minor bumps can break in 0.x)
- Vendored or monkey-patched libraries make upgrades dangerous —
  document the patches and why they exist
""",
        "git-conventions.md": """\
---
tags: [starter, coding, git]
---

# Git Conventions

Guidelines for clean, reviewable version control. This file is seeded
from a starter template — update it as you learn project-specific
conventions.

## Commit Messages
- Write concise messages that explain *why*, not just *what*
- Use imperative mood: "Add rate limiting" not "Added rate limiting"
- Keep the subject line under 72 characters; use the body for detail
- If the project uses Conventional Commits, follow the format:
  `type(scope): description` — e.g. `fix(auth): handle expired tokens`
- Common prefixes: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
- Reference issue numbers when applicable: "Fix token refresh (#42)"

## Commit Scope
- Prefer small, focused commits over large ones — easier to review,
  bisect, and revert
- Each commit should be a single logical change that passes tests
- Don't mix refactoring with feature changes in the same commit
- Don't mix formatting/linting fixes with logic changes
- If a task touches multiple files, group changes by purpose rather
  than by file

## Branch Hygiene
- Work on feature branches, not directly on main/master
- Name branches descriptively: `feature/add-rate-limiting`,
  `fix/token-refresh`, `refactor/db-connection-pool`
- Rebase or merge from the base branch before creating a PR to
  minimise merge conflicts
- Delete branches after merging — stale branches add confusion
- If a branch falls far behind the base, rebase incrementally
  rather than in one large, conflict-heavy operation

## Pull Requests
- Keep PRs focused on a single feature, fix, or refactor — large
  PRs are hard to review and slow to merge
- Write a clear description: what changed, why, and how to test it
- If the PR is not ready for review, mark it as a draft
- Address all review comments before requesting re-review
- Prefer squash-merge for feature branches to keep history clean
  (unless the project prefers merge commits)

## What Not to Commit
- Never commit secrets, API keys, `.env` files, or credentials —
  if accidentally committed, rotate the secret immediately (git
  history is permanent even after force-push)
- Avoid committing generated files, build artifacts, or large
  binaries — use `.gitignore` to exclude them
- Don't commit editor/IDE config (`.vscode/`, `.idea/`) unless the
  project deliberately shares settings
- Keep `.gitignore` up to date as the project evolves; review it
  when adding new tools or frameworks

## Conflict Resolution
- When resolving merge conflicts, understand both sides before
  choosing — don't blindly accept "ours" or "theirs"
- After resolving, re-run tests to ensure the merge didn't break
  anything
- If a conflict is large or touches critical code, consider
  breaking the merge into smaller steps or asking the original
  author for guidance

## Working with History
- Use `git log --oneline` or `git log --graph` to understand
  recent history before making changes
- Prefer `git rebase` for local-only commits to keep history
  linear; avoid rebasing commits already pushed to shared branches
- Never force-push to shared branches (main, develop) — it
  rewrites history for everyone
- Use `git stash` to save work-in-progress before switching
  branches — don't leave uncommitted changes scattered around

## Pre-commit Hooks and CI
- Respect pre-commit hooks: if a hook fails, fix the underlying
  issue rather than skipping it with `--no-verify`
- Ensure code passes linting and formatting before committing —
  most projects have hooks or CI checks for this
- Run the relevant test suite locally before pushing to avoid
  blocking the CI pipeline for others
""",
    },
    "code-review": {
        "review-checklist.md": """\
---
tags: [starter, code-review, checklist]
---

# Review Checklist

Structured checklist for code review tasks. This file is seeded from
a starter template — update it as you refine your review process.

## Correctness
- Does the code do what the PR description claims?
- Are edge cases handled (empty inputs, nulls, boundary values)?
- Are error paths handled gracefully (no silent swallowing)?
- Are return values and types consistent with the function contract?
- Is state mutation intentional and clearly documented?

## Security
- No hardcoded secrets, tokens, or credentials?
- Input validation present for external data?
- SQL queries parameterized (no string interpolation)?
- Authentication and authorization checks in place for protected paths?
- Sensitive data not leaked in logs, error messages, or stack traces?

## Performance
- No unnecessary database queries in loops (N+1 problem)?
- Large collections handled with pagination or streaming?
- Async operations used where appropriate (no blocking in event loops)?
- Expensive computations cached or deferred where practical?
- Resource handles (files, connections, cursors) properly closed/released?

## Maintainability
- Code is readable without requiring author explanation?
- Functions and variables have clear, descriptive names?
- No dead code, commented-out blocks, or TODO items left behind?
- Functions have a single responsibility and reasonable length?
- Complex logic has explanatory comments for *why*, not just *what*?

## Error Handling
- Exceptions are specific (no bare `except:` or `except Exception`)?
- Error messages are actionable and include relevant context?
- Partial failures leave the system in a consistent state?
- External service calls have timeouts and retry/fallback logic?

## API and Interface Design
- Public APIs are backward-compatible (no breaking changes without notice)?
- Method signatures are consistent with project conventions?
- New configuration options have sensible defaults?
- Breaking changes are documented and versioned?

## Dependencies
- New dependencies are justified and widely maintained?
- Dependency versions are pinned or constrained appropriately?
- No duplicate functionality with existing dependencies?

## Testing
- New functionality has corresponding tests?
- Tests cover both happy path and error cases?
- Existing tests still pass (no regressions)?
- Tests are deterministic (no reliance on timing, external services, or order)?
- Test names clearly describe the scenario and expected outcome?
""",
        "review-process.md": """\
---
tags: [starter, code-review, process]
---

# Review Process

Guidelines for conducting effective code reviews. This file is seeded
from a starter template — update it as you develop project-specific
review conventions.

## Before You Start
- Read the PR description and linked issue/task to understand intent
- Check the diff size — large PRs may need to be reviewed file-by-file
  or split into smaller reviews
- Identify which files are structural changes vs. cosmetic/mechanical

## Review Order
- Start with the most critical files (public APIs, data models, security
  boundaries) before moving to implementation details
- Review tests alongside the code they exercise, not as an afterthought
- Read new files top-down; read modified files by focusing on the diff
  hunks in context

## Giving Feedback
- Distinguish blocking issues from suggestions — prefix with "nit:" or
  "suggestion:" for non-blocking comments
- Explain *why* something is a problem, not just *what* to change
- Offer concrete alternatives when requesting changes
- Acknowledge good patterns — positive feedback reinforces quality

## Scope Discipline
- Review only what's in the PR — don't request unrelated refactors
- If you notice pre-existing issues, file them separately rather than
  blocking the current review
- Style preferences that aren't in the linter/formatter config are
  suggestions, not requirements

## Common Review Pitfalls
- Nitpicking formatting that the linter should enforce automatically
- Requesting changes that contradict the project's established patterns
- Approving without reading tests or verifying edge case coverage
- Failing to check for missing error handling in new code paths
""",
    },
    "qa": {
        "testing-patterns.md": """\
---
tags: [starter, qa, testing]
---

# Testing Patterns

Guidelines for effective testing strategies. This file is seeded from
a starter template — update it as you discover project-specific patterns.

## Test Pyramid
- Prefer unit tests for pure logic and data transformations — they're
  fast, reliable, and pinpoint failures precisely
- Use integration tests for critical paths (API endpoints, database
  queries, service boundaries) where components must work together
- Reserve end-to-end tests for key user workflows only — they're slow,
  flaky, and expensive to maintain
- Aim for roughly 70% unit / 20% integration / 10% end-to-end as a
  starting point; adjust based on project risk profile
- If a bug slips through, add the cheapest test that would have caught
  it (prefer unit over integration over e2e)

## Test Design Principles
- Each test should verify one behavior (single assertion principle) —
  multiple assertions are fine if they verify a single logical outcome
- Use descriptive test names that explain the scenario and expected
  outcome: `test_expired_token_returns_401` not `test_token`
- Follow Arrange-Act-Assert: set up state, perform the action, check
  the result — keep the three phases visually distinct
- Test public behavior, not implementation details — tests coupled to
  internals break on every refactor without catching real bugs
- Write the test first when fixing a bug: reproduce the failure, then
  fix it, ensuring the test goes from red to green

## Naming Conventions
- Group related tests in classes: `TestTokenRefresh`, `TestRateLimiter`
- Use a consistent naming pattern:
  `test_{method_or_feature}_{scenario}_{expected_outcome}`
- Prefix integration tests or slow tests with markers so they can be
  run selectively (e.g. `@pytest.mark.integration`)
- Name fixtures descriptively: `authenticated_client` not `client2`
- Name test files to mirror the module under test:
  `test_orchestrator.py` tests `orchestrator.py`

## Fixtures and Setup
- Use fixtures for shared setup (database connections, temp directories,
  authenticated clients) — avoid duplicating setup across tests
- Scope fixtures appropriately: session for expensive one-time setup
  (e.g. CLI auth check), function (default) for per-test isolation
- Always clean up resources in fixtures using `yield` with teardown:
  create in the setup phase, `yield` the resource, close/cleanup after
- Prefer creating real lightweight objects (in-memory DBs, temp dirs)
  over elaborate mocks when feasible — they catch more bugs
- Use factory fixtures when tests need variations of the same object:
  `make_task(status="running")` is clearer than many similar fixtures

## Mocking and Stubbing
- Mock at boundaries (external APIs, file systems, clocks, network) —
  not internal implementation details
- Use `AsyncMock` for coroutines and `MagicMock` for synchronous code;
  mixing them causes subtle `TypeError` or `RuntimeWarning` issues
- Prefer dependency injection over monkey-patching: pass the dependency
  as a parameter rather than patching it at import time
- When using `patch()`, patch where the name is *looked up*, not where
  it's *defined*: `patch("mymodule.requests.get")` not
  `patch("requests.get")`
- Verify mock interactions sparingly — assert on outcomes (return values,
  state changes) rather than call counts when possible
- Scripted/deterministic test doubles (pre-programmed response queues)
  are excellent for testing multi-step interaction loops without real
  service calls

## Async Testing
- Use `asyncio_mode = "auto"` in pytest config to avoid decorating
  every async test with `@pytest.mark.asyncio`
- Async fixtures need `async def` and work with `yield` just like
  synchronous ones — the test framework handles the event loop
- Never use `asyncio.run()` inside tests that already have a running
  event loop — let the framework manage the loop
- Use `asyncio.wait_for()` with a timeout in tests that await
  potentially-hanging operations to prevent test suite hangs
- For testing concurrent behavior, use `asyncio.gather()` or
  `asyncio.TaskGroup` to run operations in parallel and assert on
  the combined result

## Database and State Testing
- Use temporary databases (in-memory SQLite, temp-dir-backed files)
  for isolation — never test against shared or production data
- Always initialize and tear down the database in fixtures:
  `await db.initialize()` / `yield db` / `await db.close()`
- Test the full lifecycle: create → read → update → delete, not just
  individual operations in isolation
- Verify constraint enforcement: unique violations, foreign keys,
  not-null constraints should raise specific errors
- Reset state between tests — residual data from a prior test is a
  common source of flaky failures

## Error and Edge-Case Testing
- Test both the happy path and error paths for every function —
  untested error handling is effectively untested code
- Use `pytest.raises(ExceptionType, match="pattern")` to verify both
  the exception type and message content
- Test boundary values: empty collections, zero, negative numbers,
  maximum lengths, `None` inputs
- Verify that partial failures leave the system in a consistent state
  (e.g. a transaction rolls back on error, a lock is released)
- Test timeout and cancellation behavior for async operations

## Common Pitfalls
- Tests that depend on execution order are fragile — each test should
  be fully independent and idempotent
- Tests that sleep for fixed durations are slow and flaky — use polling
  with a timeout, event-based waits, or `asyncio.Event`
- Over-mocking hides bugs — if you mock everything, you're only
  testing that your mocks work, not that the code works
- Catching too many exceptions in test helpers masks failures — let
  unexpected exceptions propagate so they surface as test errors
- Shared mutable state between tests (module-level variables, class
  attributes) causes order-dependent failures that only appear in CI
- Forgetting to `await` async assertions — the test passes because
  the coroutine object is truthy, not because the assertion succeeded
- Tests that assert on exact error messages are brittle — match on
  key phrases or error codes instead of full strings
""",
        "qa-process.md": """\
---
tags: [starter, qa, process]
---

# QA Process

Guidelines for planning, maintaining, and improving a test suite. This
file is seeded from a starter template — update it as you develop
project-specific QA practices.

## Test Planning
- Before writing code, identify which behaviors need tests — think
  about inputs, outputs, side effects, and error conditions
- When fixing a bug, write a failing test first that reproduces the
  issue, then fix the code — this prevents regressions
- Prioritise testing critical paths (auth, payments, data integrity)
  over cosmetic or low-risk features
- For new features, define acceptance criteria that map directly to
  test cases — this keeps tests aligned with requirements
- Keep a mental model of the existing test coverage: know which areas
  are well-tested and which are under-tested

## Coverage Assessment
- Use coverage tools (`pytest-cov`, `coverage.py`) to identify untested
  code paths — but don't chase 100% as a goal
- Focus coverage on business logic and error handling — low-value
  targets (getters, `__repr__`, config loading) can be left uncovered
- Branch coverage is more informative than line coverage: a line may
  execute but only through one of its conditional paths
- Treat a sudden coverage drop in a PR as a signal to investigate —
  new code without tests is a deliberate choice that should be justified
- Untested code is not "covered by integration tests" unless those
  tests actually exercise the specific paths in question

## Test Maintenance
- Treat tests as production code: refactor when they become hard to
  read, remove when they test deleted features, update when behavior
  changes intentionally
- Consolidate duplicate setup into fixtures or helper functions —
  copy-pasted setup across tests becomes a maintenance burden
- When a test becomes flaky, fix it immediately — flaky tests erode
  trust in the entire suite and train developers to ignore failures
- Periodically review slow tests: can they be moved down the pyramid
  (integration → unit) or parallelised?
- Delete tests that no longer provide value — dead tests add noise
  and slow the suite without catching real bugs

## Debugging Failing Tests
- Read the full error message and traceback before changing code —
  the failure message usually points directly to the problem
- Reproduce the failure in isolation: run the single failing test
  before running the full suite to rule out ordering effects
- Check for environment differences between local and CI: Python
  version, OS, installed packages, available services
- For intermittent failures, look for timing dependencies, shared
  state, resource exhaustion, or non-deterministic ordering (e.g.
  dict iteration, set ordering, async task scheduling)
- Use `pytest -x` to stop on first failure and `-v` for verbose
  output when diagnosing a cascade of related failures

## CI Integration
- Run the full test suite on every PR — don't merge code with
  failing tests, even if the failures look "unrelated"
- Separate fast tests (unit) from slow tests (integration, e2e)
  using markers, so developers can run the fast suite locally and
  CI runs everything
- Set a timeout for the overall test run — a hanging test should
  fail the build, not block the CI queue indefinitely
- Cache dependencies and build artifacts to keep CI cycle time low —
  slow CI discourages frequent testing
- Run tests in parallel when possible (`pytest-xdist`) but ensure
  tests are truly independent first — shared database state is the
  most common parallelism failure

## Test Organisation
- Mirror the source tree structure in the test directory:
  `src/orchestrator.py` → `tests/test_orchestrator.py`
- Group related tests in classes to share docstrings, fixtures, and
  conceptual scope
- Keep test utility functions and custom assertions in a shared
  `conftest.py` or `tests/helpers.py` — don't scatter them across
  individual test files
- Use `conftest.py` for fixtures that are shared across multiple test
  files in the same directory; keep module-specific fixtures in the
  test file itself
- Tag tests with markers (`@pytest.mark.functional`,
  `@pytest.mark.integration`) so subsets can be run selectively
""",
    },
}

# Supervisor profile (roadmap §4.2.3, self-improvement spec §5)
# The supervisor is its own agent type — it coordinates agents, manages
# tasks, and maintains high-level project understanding.  This profile is
# written to vault/agent-types/supervisor/profile.md at startup if it doesn't exist.
SUPERVISOR_PROFILE = """\
---
id: supervisor
name: Supervisor
tags: [profile, supervisor]
---

# Supervisor

## Role
You are the supervisor — the central coordinator of the agent-queue system.
You manage projects, tasks, and agent workflows. You delegate all code work
to specialized agents and never edit code directly.

Your responsibilities:
- Assign tasks to agents based on project needs and agent-type expertise
- Maintain high-level understanding of each project's state and goals
- Coordinate multi-agent workflows (feature pipelines, review sequences)
- Learn from task outcomes to improve scheduling and assignment decisions
- Detect bottlenecks, stuck agents, and systemic issues
- Synthesize project knowledge from READMEs and task completions

You work from distilled project summaries, not raw codebases. When you need
project details, read the project README or delegate to a project-aware agent.

## Config
```json
{
  "model": "claude-sonnet-4-6",
  "permission_mode": "auto"
}
```

## Tools
```json
{
  "allowed": [],
  "denied": ["file_write", "file_edit", "shell"]
}
```

## MCP Servers
```json
{}
```

## Rules
- Never edit code directly — always delegate to an agent via task creation
- Prefer creating focused, parallel tasks over large monolithic ones
- Include all relevant context in task descriptions (file paths, requirements,
  error messages, design decisions) so agents can work independently
- When a task fails, analyze the failure before blindly retrying
- Update project memory when significant state changes occur
- Verify actions after taking them (list tasks after creating, read rules
  after saving)

## Reflection
After coordinating work, consider:
- Did task assignments match agent-type strengths?
- Were task descriptions self-contained enough for agents to succeed?
- Did any tasks fail due to missing context I could have provided?
- Are there recurring patterns in task outcomes worth remembering?
- Should project summaries be updated based on completed work?

## Install
```json
{
  "npm": [],
  "pip": [],
  "commands": []
}
```
"""


# ---------------------------------------------------------------------------
# Phase 2: Migrate passive rules to vault memory files (playbooks spec §13)
# ---------------------------------------------------------------------------


def migrate_passive_rules_to_memory(data_dir: str) -> dict:
    """Move passive rules from playbook directories to vault memory guidance.

    Phase 2 migration (playbooks spec §13): passive rules serve a different
    purpose — contextual guidance rather than workflow automation.  In the
    new architecture, they become memory files in the appropriate scope
    within the vault, where they're surfaced through memory search rather
    than a separate rule mechanism.

    Moves passive rule files:

    - **Global:** ``vault/system/playbooks/{id}.md`` →
      ``vault/system/memory/guidance/{id}.md``
    - **Project:** ``vault/projects/{pid}/playbooks/{id}.md`` →
      ``vault/projects/{pid}/memory/guidance/{id}.md``

    The operation is **idempotent**:

    * Files already present at the destination are skipped.
    * Only files with ``type: passive`` in their YAML frontmatter are moved.
    * The frontmatter is updated: ``hooks`` is removed, ``tags`` is added.
    * Source files are removed only after a successful write.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        Dict with ``moved``, ``skipped``, and ``errors`` counts plus a
        ``details`` list of per-file log messages.
    """
    import yaml

    stats: dict = {"moved": 0, "skipped": 0, "errors": 0, "details": []}

    # Build list of (playbooks_dir, scope_label, guidance_dest_dir) to scan
    dirs_to_scan: list[tuple[str, str, str]] = []

    # System-scoped (global)
    system_playbooks = os.path.join(data_dir, "vault", "system", "playbooks")
    system_guidance = os.path.join(data_dir, "vault", "system", "memory", "guidance")
    if os.path.isdir(system_playbooks):
        dirs_to_scan.append((system_playbooks, "system", system_guidance))

    # Project-scoped
    projects_dir = os.path.join(data_dir, "vault", "projects")
    if os.path.isdir(projects_dir):
        for project_id in sorted(os.listdir(projects_dir)):
            playbooks_dir = os.path.join(projects_dir, project_id, "playbooks")
            guidance_dir = os.path.join(projects_dir, project_id, "memory", "guidance")
            if os.path.isdir(playbooks_dir):
                dirs_to_scan.append((playbooks_dir, project_id, guidance_dir))

    for playbooks_dir, scope_label, guidance_dir in dirs_to_scan:
        for filename in sorted(os.listdir(playbooks_dir)):
            if not filename.endswith(".md"):
                continue

            src_path = os.path.join(playbooks_dir, filename)
            try:
                with open(src_path, encoding="utf-8") as f:
                    raw = f.read()
            except Exception as e:
                stats["errors"] += 1
                detail = f"ERROR {scope_label}/{filename}: read failed: {e}"
                stats["details"].append(detail)
                logger.warning("Passive rule migration: %s", detail)
                continue

            # Parse frontmatter to check rule type
            if not raw.startswith("---"):
                continue  # Not a rule file — skip silently
            parts = raw.split("---", 2)
            if len(parts) < 3:
                continue
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                continue

            if meta.get("type") != "passive":
                continue  # Not a passive rule — skip

            dest_path = os.path.join(guidance_dir, filename)

            # Idempotent: skip if destination already exists
            if os.path.exists(dest_path):
                stats["skipped"] += 1
                detail = f"SKIP {scope_label}/{filename}: already at destination"
                stats["details"].append(detail)
                logger.debug("Passive rule migration: %s", detail)
                continue

            # Update frontmatter: remove hooks, add tags
            meta.pop("hooks", None)
            if "tags" not in meta:
                meta["tags"] = ["guidance", "passive-rule"]
            elif "guidance" not in meta["tags"]:
                meta["tags"].append("guidance")

            # Rebuild file content with updated frontmatter
            new_frontmatter = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
            new_content = f"---\n{new_frontmatter}\n---\n{parts[2]}"

            try:
                os.makedirs(guidance_dir, exist_ok=True)
                with open(dest_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                os.remove(src_path)
                stats["moved"] += 1
                detail = f"MOVE {scope_label}/{filename}: playbooks/ → memory/guidance/"
                stats["details"].append(detail)
                logger.info("Migrated passive rule %s → %s", src_path, dest_path)
            except Exception as e:
                stats["errors"] += 1
                detail = f"ERROR {scope_label}/{filename}: {e}"
                stats["details"].append(detail)
                logger.warning("Failed to migrate passive rule %s: %s", src_path, e)

    if stats["moved"] or stats["errors"]:
        logger.info(
            "Passive rule migration complete: %d moved, %d skipped, %d errors",
            stats["moved"],
            stats["skipped"],
            stats["errors"],
        )

    return stats


# ---------------------------------------------------------------------------
# Static vault subdirectories (always created at startup)
# ---------------------------------------------------------------------------

_STATIC_DIRS: list[str] = [
    # Obsidian configuration
    "vault/.obsidian",
    # System-scoped directory (shared with supervisor)
    "vault/system",
    # Supervisor profile, playbooks, and memory
    "vault/agent-types/supervisor/playbooks",
    "vault/agent-types/supervisor/memory",
    # Agent-types root (subdirs created per profile)
    "vault/agent-types",
    # Projects root (subdirs created per project)
    "vault/projects",
    # Templates for new profiles, playbooks, etc.
    "vault/templates",
]


def ensure_vault_layout(data_dir: str) -> None:
    """Create the static vault directory structure under *data_dir*.

    This covers the directories that exist regardless of which profiles or
    projects are configured:

    - ``vault/system/``
    - ``vault/agent-types/supervisor/playbooks/``
    - ``vault/agent-types/supervisor/memory/``
    - ``vault/agent-types/supervisor/profile.md``
    - ``vault/agent-types/``
    - ``vault/projects/``
    - ``vault/templates/``
    - ``vault/.obsidian/``

    Also writes default template files (profile, playbook, starter knowledge
    packs) into ``vault/templates/`` if they don't already exist.  See
    :func:`ensure_default_templates` for details.

    Installs default system playbooks (``task-outcome.md``, etc.) into
    ``vault/system/playbooks/`` if they don't already exist.  See
    :func:`ensure_default_playbooks` for details.

    The supervisor's own ``profile.md`` (roadmap §4.2.3) is written to
    ``vault/agent-types/supervisor/profile.md`` if it doesn't already exist.
    See :func:`ensure_supervisor_profile` for details.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
    """
    for subdir in _STATIC_DIRS:
        path = os.path.join(data_dir, subdir)
        os.makedirs(path, exist_ok=True)

    ensure_default_templates(data_dir)
    ensure_default_playbooks(data_dir)
    ensure_supervisor_profile(data_dir)
    logger.info("Vault directory structure ensured at %s/vault", data_dir)


def ensure_default_templates(data_dir: str) -> dict:
    """Write default template files into ``vault/templates/`` if they don't exist.

    Creates the following template files (roadmap §4.2.2):

    - ``vault/templates/profile-template.md`` — starter template for new
      agent profiles following the hybrid markdown format (profiles spec §2).
    - ``vault/templates/playbook-template.md`` — starter template for new
      playbooks (playbook spec §4).
    - ``vault/templates/knowledge/{type}/*.md`` — starter knowledge packs
      for common agent types: ``coding``, ``code-review``, ``qa``
      (profiles spec §4).

    The operation is **idempotent**: existing files are never overwritten.
    This allows users to customise templates without losing their changes
    on the next startup.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        Dict with ``created`` (list of relative paths written) and
        ``skipped`` (list of relative paths that already existed).
    """
    templates_dir = os.path.join(data_dir, "vault", "templates")
    os.makedirs(templates_dir, exist_ok=True)

    result: dict = {"created": [], "skipped": []}

    def _write_if_missing(rel_path: str, content: str) -> None:
        """Write *content* to *rel_path* under templates_dir if it doesn't exist."""
        full_path = os.path.join(templates_dir, rel_path)
        if os.path.exists(full_path):
            result["skipped"].append(rel_path)
            logger.debug("Template already exists, skipping: %s", rel_path)
            return
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        result["created"].append(rel_path)
        logger.debug("Created default template: %s", rel_path)

    # Profile and playbook templates
    _write_if_missing("profile-template.md", PROFILE_TEMPLATE)
    _write_if_missing("playbook-template.md", PLAYBOOK_TEMPLATE)

    # Starter knowledge packs (profiles spec §4)
    for agent_type, files in _STARTER_KNOWLEDGE.items():
        for filename, content in files.items():
            rel_path = os.path.join("knowledge", agent_type, filename)
            _write_if_missing(rel_path, content)

    if result["created"]:
        logger.info(
            "Created %d default template(s) in %s",
            len(result["created"]),
            templates_dir,
        )

    return result


def ensure_default_playbooks(data_dir: str) -> dict:
    """Install default playbook files into ``vault/system/playbooks/`` if absent.

    Copies bundled playbook markdown files from ``src/prompts/default_playbooks/``
    into the vault's system playbook directory.  These playbooks are compiled at
    first use by the :class:`~src.playbooks.compiler.PlaybookCompiler`.

    Default playbooks (playbooks spec §12):

    - ``task-outcome.md`` — consolidates post-action reflection, spec-drift
      detection, and error-recovery monitoring into a single playbook triggered
      on ``task.completed`` and ``task.failed``.
    - ``system-health-check.md`` — checks for stuck tasks, blocked tasks with
      no resolution path, and unresponsive agents every 30 minutes.
      Triggered on ``timer.30m``.
    - ``codebase-inspector.md`` — periodically inspects a random section of the
      codebase for quality issues, security risks, and documentation gaps.
      Triggered on ``timer.4h``.
    - ``dependency-audit.md`` — runs dependency vulnerability checks
      (pip-audit + check-outdated-deps) and creates tasks for critical issues.
      Triggered on ``timer.24h``.

    The operation is **idempotent**: existing files in the vault are never
    overwritten.  Users can customise or disable playbooks without losing
    changes on restart.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        Dict with ``created`` (list of filenames written) and
        ``skipped`` (list of filenames that already existed).
    """
    defaults_dir = os.path.join(os.path.dirname(__file__), "prompts", "default_playbooks")
    playbooks_dir = os.path.join(data_dir, "vault", "system", "playbooks")
    os.makedirs(playbooks_dir, exist_ok=True)

    result: dict = {"created": [], "skipped": []}

    if not os.path.isdir(defaults_dir):
        logger.debug("No default playbooks directory found at %s", defaults_dir)
        return result

    for filename in sorted(os.listdir(defaults_dir)):
        if not filename.endswith(".md"):
            continue
        src_path = os.path.join(defaults_dir, filename)
        dst_path = os.path.join(playbooks_dir, filename)

        if os.path.exists(dst_path):
            result["skipped"].append(filename)
            logger.debug("Default playbook already exists, skipping: %s", filename)
            continue

        shutil.copy2(src_path, dst_path)
        result["created"].append(filename)
        logger.debug("Installed default playbook: %s", filename)

    if result["created"]:
        logger.info(
            "Installed %d default playbook(s) to %s: %s",
            len(result["created"]),
            playbooks_dir,
            ", ".join(result["created"]),
        )

    return result


def ensure_supervisor_profile(data_dir: str) -> bool:
    """Write the supervisor profile to ``vault/agent-types/supervisor/profile.md`` if absent.

    The supervisor is its own agent type (roadmap §4.2.3, self-improvement
    spec §5).  This function writes the default :data:`SUPERVISOR_PROFILE`
    to the vault so that the profile sync system can pick it up and upsert
    it into the ``agent_profiles`` database table at startup.

    The operation is **idempotent**: if the file already exists it is not
    overwritten, preserving any user customisations.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        ``True`` if the file was created, ``False`` if it already existed.
    """
    profile_path = os.path.join(data_dir, "vault", "agent-types", "supervisor", "profile.md")

    if os.path.exists(profile_path):
        logger.debug("Supervisor profile already exists, skipping: %s", profile_path)
        return False

    os.makedirs(os.path.dirname(profile_path), exist_ok=True)
    with open(profile_path, "w", encoding="utf-8") as f:
        f.write(SUPERVISOR_PROFILE)

    logger.info("Created supervisor profile: %s", profile_path)
    return True


def ensure_vault_profile_dirs(data_dir: str, profile_id: str) -> None:
    """Create vault subdirectories for an agent-type profile.

    Creates the ``vault/agent-types/{profile_id}/`` tree with ``playbooks/``
    and ``memory/`` subdirectories, as described in the vault spec §2.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        profile_id: The profile identifier (e.g. ``coding``).
    """
    base = os.path.join(data_dir, "vault", "agent-types", profile_id)
    os.makedirs(os.path.join(base, "playbooks"), exist_ok=True)
    os.makedirs(os.path.join(base, "memory"), exist_ok=True)
    os.makedirs(os.path.join(base, "memory", "guidance"), exist_ok=True)


def copy_starter_knowledge(data_dir: str, profile_id: str) -> dict:
    """Copy starter knowledge pack templates into a new profile's memory folder.

    When a new agent type is created (profile.md saved for the first time),
    this function copies matching starter knowledge from
    ``vault/templates/knowledge/{profile_id}/`` to
    ``vault/agent-types/{profile_id}/memory/``.  These files are already
    tagged ``#starter`` in their frontmatter (profiles spec §4), so agents
    and users can identify and eventually replace them.

    The operation is **idempotent**: existing files in the destination are
    never overwritten.  This ensures that if a user has already customised
    a starter file, re-running this function (e.g. on restart) does not
    destroy their edits.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        profile_id: The profile identifier (e.g. ``coding``).

    Returns:
        Dict with:
        - ``copied`` (list[str]): Relative filenames that were copied.
        - ``skipped`` (list[str]): Relative filenames already present.
        - ``source`` (str): The source template directory path.
    """
    templates_dir = os.path.join(data_dir, "vault", "templates", "knowledge", profile_id)
    memory_dir = os.path.join(data_dir, "vault", "agent-types", profile_id, "memory")

    result: dict = {"copied": [], "skipped": [], "source": templates_dir}

    if not os.path.isdir(templates_dir):
        logger.debug(
            "No starter knowledge pack for profile '%s' (no directory at %s)",
            profile_id,
            templates_dir,
        )
        return result

    os.makedirs(memory_dir, exist_ok=True)

    for filename in sorted(os.listdir(templates_dir)):
        src_path = os.path.join(templates_dir, filename)
        if not os.path.isfile(src_path):
            continue

        dst_path = os.path.join(memory_dir, filename)
        if os.path.exists(dst_path):
            result["skipped"].append(filename)
            logger.debug(
                "Starter knowledge file already exists, skipping: %s → %s",
                filename,
                dst_path,
            )
            continue

        shutil.copy2(src_path, dst_path)
        result["copied"].append(filename)
        logger.debug(
            "Copied starter knowledge file: %s → %s",
            src_path,
            dst_path,
        )

    if result["copied"]:
        logger.info(
            "Copied %d starter knowledge file(s) for profile '%s': %s",
            len(result["copied"]),
            profile_id,
            ", ".join(result["copied"]),
        )

    return result


def copy_project_memory_to_vault(data_dir: str, project_id: str) -> bool:
    """Copy project memory files from ``memory/{project_id}/`` to the vault.

    Part of vault migration Phase 1 (spec §6).  Copies the following files
    from the legacy ``memory/{project_id}/`` directory into the vault's
    per-project memory directory:

    - ``profile.md`` → ``vault/projects/{project_id}/memory/profile.md``
    - ``factsheet.md`` → ``vault/projects/{project_id}/memory/factsheet.md``
    - ``knowledge/`` → ``vault/projects/{project_id}/memory/knowledge/``

    Files are **copied** (not moved) because the old paths are still used by
    the v1 memory system during the transition period.

    Explicitly excluded: ``tasks/`` (already migrated in Phase 0) and
    ``rules/`` (migrated in roadmap 1.2.2).

    The operation is **idempotent**:

    * If the source directory does not exist, nothing happens (returns ``False``).
    * If a destination file already exists and is at least as recent as the
      source, that file is skipped.
    * If the source file is newer than the destination, the destination is
      updated.
    * Calling the function again after a successful copy is a safe no-op
      (unless source files have been updated).

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        project_id: The project identifier (e.g. ``mech-fighters``).

    Returns:
        ``True`` if any files were copied/updated, ``False`` if skipped.
    """
    source = os.path.join(data_dir, "memory", project_id)
    dest = os.path.join(data_dir, "vault", "projects", project_id, "memory")

    if not os.path.isdir(source):
        logger.debug(
            "Memory copy for %s: source %s does not exist, skipping",
            project_id,
            source,
        )
        return False

    os.makedirs(dest, exist_ok=True)

    copied_any = False

    # Copy top-level memory files (profile.md, factsheet.md)
    for filename in ("profile.md", "factsheet.md"):
        src_file = os.path.join(source, filename)
        dst_file = os.path.join(dest, filename)

        if not os.path.isfile(src_file):
            continue

        if os.path.exists(dst_file):
            # Only update if source is newer
            src_mtime = os.path.getmtime(src_file)
            dst_mtime = os.path.getmtime(dst_file)
            if src_mtime <= dst_mtime:
                logger.debug(
                    "Memory copy for %s: %s is up to date, skipping",
                    project_id,
                    filename,
                )
                continue

        shutil.copy2(src_file, dst_file)
        copied_any = True
        logger.debug("Copied memory file %s → %s", src_file, dst_file)

    # Copy knowledge/ directory contents
    knowledge_src = os.path.join(source, "knowledge")
    knowledge_dst = os.path.join(dest, "knowledge")

    if os.path.isdir(knowledge_src):
        os.makedirs(knowledge_dst, exist_ok=True)

        for dirpath, _dirnames, filenames in os.walk(knowledge_src):
            rel_dir = os.path.relpath(dirpath, knowledge_src)
            dst_dir = os.path.join(knowledge_dst, rel_dir) if rel_dir != "." else knowledge_dst
            os.makedirs(dst_dir, exist_ok=True)

            for fname in filenames:
                src_file = os.path.join(dirpath, fname)
                dst_file = os.path.join(dst_dir, fname)

                if os.path.exists(dst_file):
                    src_mtime = os.path.getmtime(src_file)
                    dst_mtime = os.path.getmtime(dst_file)
                    if src_mtime <= dst_mtime:
                        logger.debug(
                            "Memory copy for %s: knowledge/%s is up to date, skipping",
                            project_id,
                            fname,
                        )
                        continue

                shutil.copy2(src_file, dst_file)
                copied_any = True
                logger.debug("Copied knowledge file %s → %s", src_file, dst_file)

    if copied_any:
        logger.info(
            "Copied project memory files for %s from %s to %s",
            project_id,
            source,
            dest,
        )
    return copied_any


def ensure_vault_project_dirs(data_dir: str, project_id: str) -> None:
    """Create vault subdirectories for a project.

    Creates the ``vault/projects/{project_id}/`` tree with subdirectories
    for memory, playbooks, notes, references, and overrides, as described
    in the vault spec §2.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        project_id: The project identifier (e.g. ``mech-fighters``).
    """
    base = os.path.join(data_dir, "vault", "projects", project_id)
    for subdir in (
        "memory/knowledge",
        "memory/insights",
        "memory/guidance",
        "playbooks",
        "notes",
        "references",
        "overrides",
    ):
        os.makedirs(os.path.join(base, subdir), exist_ok=True)


# ---------------------------------------------------------------------------
# Startup auto-migration helpers (spec §6 — smooth transition)
# ---------------------------------------------------------------------------


def has_legacy_data(data_dir: str) -> bool:
    """Check whether legacy data paths exist that should be migrated.

    Returns ``True`` if any of these contain actual content:

    * ``notes/{project}/`` — any project subdirectory with files
    * ``memory/{project}/rules/`` — any project or global rules directory
    * ``memory/.obsidian/`` — Obsidian config at the old location
    * ``memory/{project}/`` — any project memory directory with files

    This is used at startup to decide whether to trigger an automatic
    migration for existing installs (spec §6).
    """
    # Check notes/{project}/ directories
    notes_root = os.path.join(data_dir, "notes")
    if os.path.isdir(notes_root):
        for entry in os.listdir(notes_root):
            entry_path = os.path.join(notes_root, entry)
            if os.path.isdir(entry_path) and any(os.scandir(entry_path)):
                return True

    # Check memory/ tree for rules dirs, obsidian config, or project files
    memory_root = os.path.join(data_dir, "memory")
    if os.path.isdir(memory_root):
        # Obsidian config at old location
        if os.path.isdir(os.path.join(memory_root, ".obsidian")):
            return True

        for entry in os.listdir(memory_root):
            if entry.startswith("."):
                continue
            entry_path = os.path.join(memory_root, entry)
            if not os.path.isdir(entry_path):
                continue
            # Check for rules/ subdirectory with .md files
            rules_path = os.path.join(entry_path, "rules")
            if os.path.isdir(rules_path):
                for f in os.listdir(rules_path):
                    if f.endswith(".md"):
                        return True
            # Check for project memory files (profile.md, factsheet.md, knowledge/)
            if entry not in _MEMORY_SPECIAL_DIRS:
                for mem_file in ("profile.md", "factsheet.md"):
                    if os.path.isfile(os.path.join(entry_path, mem_file)):
                        return True
                if os.path.isdir(os.path.join(entry_path, "knowledge")):
                    return True

    return False


def vault_has_content(data_dir: str) -> bool:
    """Check whether the vault already contains user or migrated content.

    Returns ``True`` if the vault has any regular files beyond the
    bare directory skeleton created by ``ensure_vault_layout``.  This is
    used at startup to avoid overwriting existing vault content when
    deciding whether to auto-migrate (spec §6).

    Specifically, checks for any ``.md`` files or other content files
    inside ``vault/projects/``, ``vault/system/``,
    or ``vault/agent-types/``.
    The ``vault/.obsidian/`` directory is excluded from the check
    because its presence alone doesn't indicate user-created content.
    """
    vault_root = os.path.join(data_dir, "vault")
    if not os.path.isdir(vault_root):
        return False

    # Walk the vault tree looking for any regular files
    # (excluding .obsidian/ which is config, not content)
    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Skip .obsidian/ subtree — it's Obsidian config, not vault content
        rel = os.path.relpath(dirpath, vault_root)
        if rel == ".obsidian" or rel.startswith(".obsidian" + os.sep):
            continue

        if filenames:
            return True

    return False


def vault_has_profile_markdown(data_dir: str) -> bool:
    """Check whether the vault already has any agent-type profile markdown files.

    Returns ``True`` if at least one ``vault/agent-types/*/profile.md`` file
    exists.  This is used at startup to decide whether to auto-migrate DB
    profiles to vault markdown (roadmap 4.2.4).  If any profile markdown
    already exists, we skip auto-migration to avoid interfering with
    user-managed vault content.
    """
    agent_types_dir = os.path.join(data_dir, "vault", "agent-types")
    if not os.path.isdir(agent_types_dir):
        return False

    for entry in os.listdir(agent_types_dir):
        profile_md = os.path.join(agent_types_dir, entry, "profile.md")
        if os.path.isfile(profile_md):
            return True

    return False


# ---------------------------------------------------------------------------
# Consolidated vault migration (spec §6)
# ---------------------------------------------------------------------------

# Directories inside memory/ that are NOT project IDs
_MEMORY_SPECIAL_DIRS = frozenset({"global", ".obsidian"})


def _discover_project_ids(data_dir: str) -> list[str]:
    """Discover project IDs from legacy filesystem locations.

    Scans ``notes/`` and ``memory/`` directories for subdirectories that
    represent projects.  Returns a sorted, deduplicated list.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).

    Returns:
        Sorted list of project identifiers discovered on disk.
    """
    project_ids: set[str] = set()

    # From notes/{project_id}/
    notes_root = os.path.join(data_dir, "notes")
    if os.path.isdir(notes_root):
        for entry in os.listdir(notes_root):
            if os.path.isdir(os.path.join(notes_root, entry)):
                project_ids.add(entry)

    # From memory/{project_id}/ (excluding special dirs)
    memory_root = os.path.join(data_dir, "memory")
    if os.path.isdir(memory_root):
        for entry in os.listdir(memory_root):
            if entry in _MEMORY_SPECIAL_DIRS or entry.startswith("."):
                continue
            if os.path.isdir(os.path.join(memory_root, entry)):
                project_ids.add(entry)

    return sorted(project_ids)


def _scan_obsidian_migration(data_dir: str) -> dict:
    """Preview what ``migrate_obsidian_config`` would do.

    Returns a dict with ``action`` ("move" or "skip") and ``reason``.
    """
    source = os.path.join(data_dir, "memory", ".obsidian")
    dest = os.path.join(data_dir, "vault", ".obsidian")

    if not os.path.isdir(source):
        return {"action": "skip", "reason": "source does not exist"}
    if os.path.exists(dest):
        return {"action": "skip", "reason": "destination already exists"}
    return {"action": "move", "source": source, "dest": dest}


def _scan_notes_migration(data_dir: str, project_id: str) -> dict:
    """Preview what ``migrate_notes_to_vault`` would do for one project.

    Returns a dict with counts: ``would_move`` and ``would_skip``.
    """
    source = os.path.join(data_dir, "notes", project_id)
    dest = os.path.join(data_dir, "vault", "projects", project_id, "notes")
    result: dict = {"would_move": 0, "would_skip": 0, "files": []}

    if not os.path.isdir(source):
        return result

    for dirpath, _dirnames, filenames in os.walk(source):
        rel_dir = os.path.relpath(dirpath, source)
        dest_dir = os.path.join(dest, rel_dir) if rel_dir != "." else dest

        for fname in filenames:
            dst_file = os.path.join(dest_dir, fname)
            rel_path = os.path.join(rel_dir, fname) if rel_dir != "." else fname
            if os.path.exists(dst_file):
                result["would_skip"] += 1
                result["files"].append(f"SKIP {rel_path}: already at destination")
            else:
                result["would_move"] += 1
                result["files"].append(f"MOVE {rel_path}")

    return result


def _scan_memory_copy(data_dir: str, project_id: str) -> dict:
    """Preview what ``copy_project_memory_to_vault`` would do for one project.

    Returns a dict with counts: ``would_copy``, ``would_update``, ``would_skip``.
    """
    source = os.path.join(data_dir, "memory", project_id)
    dest = os.path.join(data_dir, "vault", "projects", project_id, "memory")
    result: dict = {"would_copy": 0, "would_update": 0, "would_skip": 0, "files": []}

    if not os.path.isdir(source):
        return result

    def _check_file(src_file: str, dst_file: str, label: str) -> None:
        if not os.path.isfile(src_file):
            return
        if os.path.exists(dst_file):
            src_mtime = os.path.getmtime(src_file)
            dst_mtime = os.path.getmtime(dst_file)
            if src_mtime <= dst_mtime:
                result["would_skip"] += 1
                result["files"].append(f"SKIP {label}: up to date")
            else:
                result["would_update"] += 1
                result["files"].append(f"UPDATE {label}: source is newer")
        else:
            result["would_copy"] += 1
            result["files"].append(f"COPY {label}")

    # Top-level files
    for filename in ("profile.md", "factsheet.md"):
        _check_file(
            os.path.join(source, filename),
            os.path.join(dest, filename),
            filename,
        )

    # knowledge/ tree
    knowledge_src = os.path.join(source, "knowledge")
    knowledge_dst = os.path.join(dest, "knowledge")
    if os.path.isdir(knowledge_src):
        for dirpath, _dirnames, filenames in os.walk(knowledge_src):
            rel_dir = os.path.relpath(dirpath, knowledge_src)
            dst_dir = os.path.join(knowledge_dst, rel_dir) if rel_dir != "." else knowledge_dst
            for fname in filenames:
                src_file = os.path.join(dirpath, fname)
                dst_file = os.path.join(dst_dir, fname)
                label = (
                    os.path.join("knowledge", rel_dir, fname)
                    if rel_dir != "."
                    else os.path.join("knowledge", fname)
                )
                _check_file(src_file, dst_file, label)

    return result


def _scan_rule_migration(data_dir: str) -> dict:
    """Preview what ``migrate_rule_files`` would do.

    Returns a dict matching the shape of ``migrate_rule_files`` output plus
    ``would_move`` / ``would_skip`` for dry-run clarity.
    """
    result: dict = {"would_move": 0, "would_skip": 0, "details": []}
    memory_root = os.path.join(data_dir, "memory")

    if not os.path.isdir(memory_root):
        return result

    for scope_dir in sorted(os.listdir(memory_root)):
        rules_dir = os.path.join(memory_root, scope_dir, "rules")
        if not os.path.isdir(rules_dir):
            continue

        if scope_dir == "global":
            dest_dir = os.path.join(data_dir, "vault", "system", "playbooks")
        else:
            dest_dir = os.path.join(data_dir, "vault", "projects", scope_dir, "playbooks")

        for filename in sorted(os.listdir(rules_dir)):
            if not filename.endswith(".md"):
                continue
            dest_path = os.path.join(dest_dir, filename)
            if os.path.exists(dest_path):
                result["would_skip"] += 1
                result["details"].append(f"SKIP {scope_dir}/{filename}: already at destination")
            else:
                result["would_move"] += 1
                result["details"].append(f"MOVE {scope_dir}/{filename}")

    return result


def _scan_passive_rule_migration(data_dir: str) -> dict:
    """Preview what ``migrate_passive_rules_to_memory`` would do.

    Returns a dict with ``would_move`` / ``would_skip`` counts and
    ``details`` list for dry-run reporting.
    """
    import yaml

    result: dict = {"would_move": 0, "would_skip": 0, "details": []}

    dirs_to_scan: list[tuple[str, str, str]] = []

    system_playbooks = os.path.join(data_dir, "vault", "system", "playbooks")
    system_guidance = os.path.join(data_dir, "vault", "system", "memory", "guidance")
    if os.path.isdir(system_playbooks):
        dirs_to_scan.append((system_playbooks, "system", system_guidance))

    projects_dir = os.path.join(data_dir, "vault", "projects")
    if os.path.isdir(projects_dir):
        for project_id in sorted(os.listdir(projects_dir)):
            playbooks_dir = os.path.join(projects_dir, project_id, "playbooks")
            guidance_dir = os.path.join(projects_dir, project_id, "memory", "guidance")
            if os.path.isdir(playbooks_dir):
                dirs_to_scan.append((playbooks_dir, project_id, guidance_dir))

    for playbooks_dir, scope_label, guidance_dir in dirs_to_scan:
        for filename in sorted(os.listdir(playbooks_dir)):
            if not filename.endswith(".md"):
                continue
            src_path = os.path.join(playbooks_dir, filename)
            try:
                with open(src_path, encoding="utf-8") as f:
                    raw = f.read()
            except Exception:
                continue
            if not raw.startswith("---"):
                continue
            parts = raw.split("---", 2)
            if len(parts) < 3:
                continue
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                continue
            if meta.get("type") != "passive":
                continue

            dest_path = os.path.join(guidance_dir, filename)
            if os.path.exists(dest_path):
                result["would_skip"] += 1
                result["details"].append(f"SKIP {scope_label}/{filename}: already at destination")
            else:
                result["would_move"] += 1
                result["details"].append(
                    f"MOVE {scope_label}/{filename}: playbooks/ → memory/guidance/"
                )

    return result


def run_vault_migration(
    data_dir: str,
    project_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Run all vault migrations in the correct order (spec §6, Phase 1+2).

    Consolidates the individual migration operations into a single
    idempotent entry point that can be called from the CLI
    (``aq vault migrate``) or programmatically.

    **Migration order:**

    1. ``migrate_obsidian_config`` — move ``.obsidian/`` from ``memory/`` to
       ``vault/``
    2. ``ensure_vault_layout`` — create the static vault directory tree
    3. ``ensure_vault_project_dirs`` — per-project vault directories
    4. ``migrate_notes_to_vault`` — per-project notes migration
    5. ``copy_project_memory_to_vault`` — per-project memory file copy
    6. ``migrate_rule_files`` — global and per-project rule migration
    7. ``migrate_passive_rules_to_memory`` — passive rules to vault memory
       guidance (playbooks spec §13, Phase 2)

    The function is **idempotent** — safe to run multiple times.  It never
    duplicates or overwrites data that is already at the destination.

    Args:
        data_dir: The root data directory (e.g. ``~/.agent-queue``).
        project_ids: Explicit list of project IDs to migrate.  If ``None``,
            projects are auto-discovered from ``notes/`` and ``memory/``
            directories.
        dry_run: If ``True``, scan and report what *would* happen without
            making any changes.

    Returns:
        A dict with the migration report::

            {
                "dry_run": bool,
                "data_dir": str,
                "projects_discovered": [str, ...],
                "obsidian": {"action": "move"/"skip", ...},
                "notes": {"project_id": {"moved": N, "skipped": N}, ...},
                "memory": {"project_id": {"copied": N, "updated": N, "skipped": N}, ...},
                "rules": {"moved": N, "skipped": N, "errors": N, "details": [...]},
                "summary": {
                    "total_moved": int,
                    "total_copied": int,
                    "total_skipped": int,
                    "total_errors": int,
                },
                "details": [str, ...],  # human-readable log lines
            }
    """
    if project_ids is None:
        project_ids = _discover_project_ids(data_dir)

    report: dict = {
        "dry_run": dry_run,
        "data_dir": data_dir,
        "projects_discovered": list(project_ids),
        "obsidian": {},
        "notes": {},
        "memory": {},
        "rules": {},
        "summary": {
            "total_moved": 0,
            "total_copied": 0,
            "total_skipped": 0,
            "total_errors": 0,
        },
        "details": [],
    }

    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info("Starting vault migration (%s) for data_dir=%s", mode, data_dir)
    report["details"].append(f"Vault migration ({mode}) — data_dir: {data_dir}")
    report["details"].append(f"Projects: {', '.join(project_ids) or '(none discovered)'}")

    # ------------------------------------------------------------------
    # Step 1: Obsidian config
    # ------------------------------------------------------------------
    if dry_run:
        obs_scan = _scan_obsidian_migration(data_dir)
        report["obsidian"] = obs_scan
        if obs_scan["action"] == "move":
            report["summary"]["total_moved"] += 1
            report["details"].append("  .obsidian: WOULD MOVE → vault/.obsidian/")
        else:
            report["summary"]["total_skipped"] += 1
            report["details"].append(f"  .obsidian: SKIP ({obs_scan['reason']})")
    else:
        moved = migrate_obsidian_config(data_dir)
        report["obsidian"] = {
            "action": "moved" if moved else "skipped",
        }
        if moved:
            report["summary"]["total_moved"] += 1
            report["details"].append("  .obsidian: MOVED → vault/.obsidian/")
        else:
            report["summary"]["total_skipped"] += 1
            report["details"].append("  .obsidian: SKIPPED (already migrated or no source)")

    # ------------------------------------------------------------------
    # Step 2: Ensure vault layout (always needed, even for dry-run reports)
    # ------------------------------------------------------------------
    if not dry_run:
        ensure_vault_layout(data_dir)
        report["details"].append("  Vault layout: ensured")

        # Per-project directories
        for pid in project_ids:
            ensure_vault_project_dirs(data_dir, pid)
    else:
        report["details"].append("  Vault layout: WOULD ensure")

    # ------------------------------------------------------------------
    # Step 3: Notes migration (per-project)
    # ------------------------------------------------------------------
    report["details"].append("  --- Notes migration ---")
    for pid in project_ids:
        if dry_run:
            scan = _scan_notes_migration(data_dir, pid)
            report["notes"][pid] = {
                "would_move": scan["would_move"],
                "would_skip": scan["would_skip"],
            }
            report["summary"]["total_moved"] += scan["would_move"]
            report["summary"]["total_skipped"] += scan["would_skip"]
            if scan["would_move"] or scan["would_skip"]:
                report["details"].append(
                    f"  notes/{pid}: {scan['would_move']} to move, {scan['would_skip']} to skip"
                )
                for f in scan["files"]:
                    report["details"].append(f"    {f}")
        else:
            moved = migrate_notes_to_vault(data_dir, pid)
            report["notes"][pid] = {"moved": moved}
            if moved:
                report["summary"]["total_moved"] += 1
                report["details"].append(f"  notes/{pid}: migrated")
            else:
                report["details"].append(f"  notes/{pid}: skipped (no source or already done)")

    # ------------------------------------------------------------------
    # Step 4: Memory copy (per-project)
    # ------------------------------------------------------------------
    report["details"].append("  --- Memory copy ---")
    for pid in project_ids:
        if dry_run:
            scan = _scan_memory_copy(data_dir, pid)
            report["memory"][pid] = {
                "would_copy": scan["would_copy"],
                "would_update": scan["would_update"],
                "would_skip": scan["would_skip"],
            }
            report["summary"]["total_copied"] += scan["would_copy"] + scan["would_update"]
            report["summary"]["total_skipped"] += scan["would_skip"]
            total = scan["would_copy"] + scan["would_update"] + scan["would_skip"]
            if total:
                report["details"].append(
                    f"  memory/{pid}: {scan['would_copy']} to copy, "
                    f"{scan['would_update']} to update, "
                    f"{scan['would_skip']} up to date"
                )
                for f in scan["files"]:
                    report["details"].append(f"    {f}")
        else:
            copied = copy_project_memory_to_vault(data_dir, pid)
            report["memory"][pid] = {"copied": copied}
            if copied:
                report["summary"]["total_copied"] += 1
                report["details"].append(f"  memory/{pid}: copied")
            else:
                report["details"].append(f"  memory/{pid}: skipped (no source or up to date)")

    # ------------------------------------------------------------------
    # Step 5: Rule file migration
    # ------------------------------------------------------------------
    report["details"].append("  --- Rule migration ---")
    if dry_run:
        scan = _scan_rule_migration(data_dir)
        report["rules"] = {
            "would_move": scan["would_move"],
            "would_skip": scan["would_skip"],
            "details": scan["details"],
        }
        report["summary"]["total_moved"] += scan["would_move"]
        report["summary"]["total_skipped"] += scan["would_skip"]
        if scan["would_move"] or scan["would_skip"]:
            report["details"].append(
                f"  rules: {scan['would_move']} to move, {scan['would_skip']} to skip"
            )
            for d in scan["details"]:
                report["details"].append(f"    {d}")
        else:
            report["details"].append("  rules: nothing to migrate")
    else:
        rule_result = migrate_rule_files(data_dir)
        report["rules"] = rule_result
        report["summary"]["total_moved"] += rule_result["moved"]
        report["summary"]["total_skipped"] += rule_result["skipped"]
        report["summary"]["total_errors"] += rule_result["errors"]
        report["details"].append(
            f"  rules: {rule_result['moved']} moved, "
            f"{rule_result['skipped']} skipped, "
            f"{rule_result['errors']} errors"
        )
        for d in rule_result.get("details", []):
            report["details"].append(f"    {d}")

    # ------------------------------------------------------------------
    # Step 6: Passive rule migration (playbooks spec §13)
    # ------------------------------------------------------------------
    report["details"].append("  --- Passive rule → memory guidance migration ---")
    if dry_run:
        # Dry-run: scan for passive rules in playbook dirs
        passive_scan = _scan_passive_rule_migration(data_dir)
        report["passive_rules"] = passive_scan
        report["summary"]["total_moved"] += passive_scan.get("would_move", 0)
        report["summary"]["total_skipped"] += passive_scan.get("would_skip", 0)
        if passive_scan.get("would_move") or passive_scan.get("would_skip"):
            report["details"].append(
                f"  passive rules: {passive_scan['would_move']} to move, "
                f"{passive_scan['would_skip']} to skip"
            )
            for d in passive_scan.get("details", []):
                report["details"].append(f"    {d}")
        else:
            report["details"].append("  passive rules: nothing to migrate")
    else:
        passive_result = migrate_passive_rules_to_memory(data_dir)
        report["passive_rules"] = passive_result
        report["summary"]["total_moved"] += passive_result["moved"]
        report["summary"]["total_skipped"] += passive_result["skipped"]
        report["summary"]["total_errors"] += passive_result["errors"]
        report["details"].append(
            f"  passive rules: {passive_result['moved']} moved, "
            f"{passive_result['skipped']} skipped, "
            f"{passive_result['errors']} errors"
        )
        for d in passive_result.get("details", []):
            report["details"].append(f"    {d}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    s = report["summary"]
    summary_line = (
        f"Migration {'preview' if dry_run else 'complete'}: "
        f"{s['total_moved']} moved, {s['total_copied']} copied, "
        f"{s['total_skipped']} skipped, {s['total_errors']} errors"
    )
    report["details"].append(summary_line)
    logger.info(summary_line)

    return report
