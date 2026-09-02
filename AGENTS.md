# AGENTS.md — instructions for coding agents (Codex reads this file; Claude Code reads CLAUDE.md)

Everything in **CLAUDE.md** applies here too — read it first for the repo map, conventions and commands. This file exists so Codex-harness workers get the same rules.

## Testing — read this before running anything

The suite is **11,330 tests** and, until the schema-cache work lands, every fresh test database replays 58 alembic migrations (~8 s each, ~2,700 tests pay it). A full run takes **~14 minutes on 24 cores and effectively never finishes serially**. Running it casually stalls every agent on the machine.

Rules:
- **Use `aq test`, not bare `pytest`, for anything past a single file.** It takes one of the box's global test slots first, so eight agents testing at once cannot become 200 test processes, and it applies the worker cap and the default marker deselects for you. Everything that is not an `--aq-*` option goes to pytest untouched:
  ```bash
  aq test tests/test_playbook_runner.py          # the file for the module you changed
  aq test tests/test_claim_queries.py tests/test_pools.py
  aq test tests/ -k "schema_setup or run_schema"
  aq test --aq-status                            # who is holding the slots
  aq test --aq-help                              # -h belongs to pytest
  ```
  A `waiting for 1 of 2 test slot(s)` line means the box is busy, not that you are stuck. Exit code 75 means no slot came free — retry, it is not a test failure. Plain `pytest` still works for a single quick file.
- **Never run a bare `pytest` / `pytest tests/` mid-task.** Run only the tests for the code you touch.
- **Never override the worker count upward.** `-n auto` inside a session already resolves to this box's per-session share (`PYTEST_XDIST_AUTO_NUM_WORKERS`, derived from cores ÷ concurrent agents); passing a bigger `-n` bypasses the gating and is what took the box down on 2026-09-01. See [resource gating](docs/guides/resource-gating.md).
- **Find focused tests** (the layout is one file per area, `tests/test_<area>.py`, plus `tests/perf/`, `tests/llm/`, `tests/fixtures/`):
  ```bash
  aq test tests/test_playbook_runner.py            # the file for the module you changed
  aq test tests/test_claim_queries.py tests/test_pools.py    # a few related files
  aq test tests/ -k "schema_setup or run_schema"   # by name, across files
  aq test tests/test_x.py -x                       # stop at first failure while iterating
  aq test --lf                                     # re-run only what failed last time
  pytest --co -q -k <term> | tail -20              # collection only — no slot needed
  ```
- **Skip the slow-by-nature markers** unless the change is about them (real tmux, Milvus, latency budgets). `aq test` applies `-m "not tmux and not integration and not perf"` by default; pass your own `-m` (or `--aq-all-markers`) when the change *is* about them.
- **One broader run at the end of a task, not during:** the area suite for what you changed (e.g. `aq test tests/test_playbook*.py tests/test_pipeline*.py`). The whole-repo run is for CI and explicit review gates only.
- Ruff on changed files only: `ruff check <paths>`.

## Working as an aq worker

- Run `aq prime` first and follow it: it carries your task, role, rules, and the completion protocol.
- Keep the task record current: `aq task comment <id> --body "Finding: ..."` for findings/decisions/evidence; update the description when confirmed findings change how the task should be completed.
- Emergent work: file it with `aq task create` plus a `discovered-from` edge and a reason; if your task is a child of an epic, create the new task as a child of the same epic (`--parent`).
- Close explicitly: push your branch, open the PR, then `aq task close <id> --outcome pass --summary "..."`. Heartbeat (`aq task heartbeat <id>`) before anything that runs quiet for minutes.
