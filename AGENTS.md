# AGENTS.md — instructions for coding agents (Codex reads this file; Claude Code reads CLAUDE.md)

Everything in **CLAUDE.md** applies here too — read it first for the repo map, conventions and commands. This file exists so Codex-harness workers get the same rules.

## Testing — read this before running anything

The suite is **11,330 tests** and, until the schema-cache work lands, every fresh test database replays 58 alembic migrations (~8 s each, ~2,700 tests pay it). A full run takes **~14 minutes on 24 cores and effectively never finishes serially**. Running it casually stalls every agent on the machine.

Rules:
- **Never run a bare `pytest` / `pytest tests/` mid-task.** Run only the tests for the code you touch.
- **Always run in parallel:** add `-n auto` (pytest-xdist is installed). Single-file runs may go without it.
- **Find focused tests** (the layout is one file per area, `tests/test_<area>.py`, plus `tests/perf/`, `tests/llm/`, `tests/fixtures/`):
  ```bash
  pytest tests/test_playbook_runner.py -n auto -q          # the file for the module you changed
  pytest tests/test_claim_queries.py tests/test_pools.py -n auto -q   # a few related files
  pytest -k "schema_setup or run_schema" -n auto -q         # by name, across files
  pytest --co -q -k <term> | tail -20                        # discover which tests mention <term>
  pytest --lf -n auto -q                                     # re-run only what failed last time
  pytest tests/test_x.py -x -q                               # stop at first failure while iterating
  ```
- **Skip the slow-by-nature markers** unless the change is about them: `-m "not tmux and not integration and not perf"` (real tmux, Milvus, latency budgets).
- **One broader run at the end of a task, not during:** the area suite for what you changed (e.g. `pytest tests/test_playbook*.py tests/test_pipeline*.py -n auto -q`). The whole-repo run is for CI and explicit review gates only.
- Ruff on changed files only: `ruff check <paths>`.

## Working as an aq worker

- Run `aq prime` first and follow it: it carries your task, role, rules, and the completion protocol.
- Keep the task record current: `aq task comment <id> --body "Finding: ..."` for findings/decisions/evidence; update the description when confirmed findings change how the task should be completed.
- Emergent work: file it with `aq task create` plus a `discovered-from` edge and a reason; if your task is a child of an epic, create the new task as a child of the same epic (`--parent`).
- Close explicitly: push your branch, open the PR, then `aq task close <id> --outcome pass --summary "..."`. Heartbeat (`aq task heartbeat <id>`) before anything that runs quiet for minutes.
