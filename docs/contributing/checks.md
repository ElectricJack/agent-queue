# Local checks

The shortest sufficient set of commands to run before you push, chosen by what
you changed.

## Why this page exists

"Run the tests" is bad advice in this repository: the suite is large enough
that running all of it is a whole-box event, and most of it is irrelevant to
any one change. But "run less" is only safe if you know which checks a
particular change actually needs. This page is that mapping.

The rule of thumb: **lint what you edited, test the area you changed,
regenerate anything downstream of an interface you touched.** One broader
run at the end of a task, never during.

## Vocabulary

* **Focused run** — the test file (or two) for the module you changed.
* **Area suite** — the handful of files covering a subsystem, e.g.
  `tests/test_playbook*.py`.
* **Drift guard** — a test that fails when a generated artefact is stale; see
  [code generation](codegen.md).

## By what you changed

| You changed | Run |
|---|---|
| One Python module under `src/` | `ruff check <file>` and `aq test tests/test_<area>.py` |
| `src/database/tables.py` | A new Alembic revision (**locally, never against the operator's database**) plus `aq test tests/test_database.py tests/test_docs_sync.py` |
| `src/api/models/*` or a codegen router | `./scripts/regenerate-api-client.sh --offline`, `./scripts/regenerate-ts-client.sh --from-file`, `aq test tests/test_api_client_contract.py` |
| A `@click` command or command contract | `python scripts/generate-cli-command-inventory.py`, `aq test tests/test_cli_inventory.py tests/test_cli_conformance.py` |
| `src/playbooks/definition.py` | `python scripts/generate-playbook-schema.py --check`, `aq test tests/test_playbook_v2_definition.py` |
| Anything under `dashboard/src/` | `npm -w dashboard run lint`, `npm -w dashboard run typecheck`, `npm -w dashboard run test` |
| Claims, pools, formulas or the task hierarchy | `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` |
| Any page under `docs/` | `python3 docs/plans/documentation-overhaul/refresh_inventory.py --check` and the [style checklist](documentation-style.md#checking-your-page-before-you-push) |
| Added or removed **any** tracked file | `python3 docs/plans/documentation-overhaul/refresh_inventory.py --check` — the same coverage check. Do **not** regenerate the manifest; it is [foundation-owned](../documentation-map.md#who-runs-which-half). |

## The commands

### Lint

Ruff only, and only on what you touched:

```bash
ruff check src/claim_file.py
```

```text
All checks passed!
```

Settings come from [`pyproject.toml`](../../pyproject.toml): line length 100,
target `py312`, `packages/aq-client/` excluded because it is generated.
Ruff may report findings in lines your change did not touch — the repository
is not lint-clean under every rule the installed ruff enables. Fix what your
diff introduces; leave the rest.

`ruff format` is available and is what
[`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) runs, but the
repository is not blanket-formatted — format the files you are already editing,
not the ones you are not.

Optionally, let the hooks do it for you:

```bash
pre-commit install     # once
```

Both hooks are ruff: `ruff-format`, then `ruff --fix`.

### Focused tests

```bash
aq test tests/test_cli_test_runner.py
```

See [testing](testing.md) for how to find the right file, what the markers
exclude, and why `aq test` queues. The one thing to remember here: **never
raise `-n`**, and never run a bare `pytest tests/`.

### Frontend

```bash
npm -w dashboard run lint
npm -w dashboard run typecheck
npm -w dashboard run test
```

`typecheck` regenerates the TypeScript client first via a `pre` script, so it
also catches "the dashboard no longer matches the API".

### Documentation

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```

This verifies that every tracked path still has a documentation owner. It is
fast, needs no database and no daemon, and it is the check that catches
"somebody added a module nobody documents".

It does **not** fail because the committed coverage manifest has fallen behind
the tree. It prints a `note:` naming the drifted paths and the shard each was
assigned to, and exits 0. The manifest is
[foundation-owned](../documentation-map.md#who-runs-which-half) and regenerated
on a cadence; its freshness is a separate acceptance gate:

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check-artefacts
```

Run that one only if you own the overhaul's foundation/acceptance shard. On a
ticket branch, regenerating a 3,700-entry JSON is how twenty tickets collide at
delivery.

### One broader run, at the end

When the change is finished — not while you are iterating — run the area suite
for what you touched:

```bash
aq test tests/test_playbook*.py tests/test_pipeline*.py
```

The whole-repository run belongs to [CI](ci.md) and to explicit review gates.

## A worked example

A change to `src/claim_file.py` — one backend module, no API surface, no
schema, no frontend.

```bash
ruff check src/claim_file.py
```

```text
All checks passed!
```

```bash
aq test tests/test_claim_commands.py
```

```text
aq test: slot 0 of 2, -n 3
$ /…/python -m pytest -n 3 --dist loadfile -m 'not perf and not migration and not slow and not tmux and not integration' tests/test_claim_commands.py
…
============================= 85 passed in 51.82s ==============================
```

That is the whole check list. No migration, no regeneration, no build. If you
had instead changed `dashboard/src/pages/metrics/`, it would have been
`npm -w dashboard run lint`, then `typecheck`, then `test` — and nothing
Python at all.

## Inputs and outputs

| Check | Needs | Writes |
|---|---|---|
| `ruff check` | Nothing | Nothing (`ruff format` rewrites files) |
| `aq test` | `POSTGRES_TEST_DSN`, one free test slot | Throwaway databases, removed on teardown |
| `npm -w dashboard run …` | `npm install` done once | `node_modules/`, `packages/aq-ts-client/src/`, `dashboard/dist/` — all gitignored |
| `refresh_inventory.py --check` | A Git checkout | Nothing (without a `--check…` flag, two JSON files) |
| `scripts/e2e-smoke.sh` | Docker PostgreSQL, a free port | An isolated world under `~/.agent-queue-e2e` |

## State ownership

Local checks own only disposable state: test databases, tool caches and build
output, all outside the repository or gitignored inside it. The one check that
writes tracked files is `refresh_inventory.py` with no flag — which is the
foundation/acceptance shard's job, not a ticket's — and the two
regeneration scripts in [code generation](codegen.md) — those changes belong in
the same commit as the input change that caused them.

## Common failures and recovery

| Symptom | Meaning | Do |
|---|---|---|
| `aq test: POSTGRES_TEST_DSN is not set` | No test database. | [Set it up](setup.md#postgresql-for-tests). Nothing ran. |
| exit `75` from `aq test` | Every slot busy for the whole timeout. | Retry. Not a test failure. |
| `aq test: no tests were collected` | Your paths or markers excluded everything. | `--aq-dry-run` shows the exact command that ran. |
| `ruff check` flags a generated file | You are linting something you should not. | `packages/aq-client/` is excluded by config; do not lint it by path either. |
| `stale artefact(s)` from `--check-artefacts` | The foundation-owned manifest has fallen behind the tree. | Regenerate with no flag — but only if you own that shard. It is not a ticket's failure, and plain `--check` does not report it. |
| A frontend command cannot resolve `@aq/ts-client` | The generated client is missing. | `./scripts/regenerate-ts-client.sh --from-file` |

## Related pages

* [Testing](testing.md) — markers, fixtures, and the `aq test` wrapper in full.
* [Code generation](codegen.md) — every regeneration command and its guard.
* [CI](ci.md) — what runs after you push, and what does not.
* [Pull requests and delivery](pull-requests.md) — what to do once these pass.
* [Documentation style](documentation-style.md) — the checklist for prose.

## Source and tests

[`src/cli/test_runner.py`](../../src/cli/test_runner.py),
[`.pre-commit-config.yaml`](../../.pre-commit-config.yaml),
[`pyproject.toml`](../../pyproject.toml),
[`docs/plans/documentation-overhaul/refresh_inventory.py`](../plans/documentation-overhaul/refresh_inventory.py).

```bash
aq test tests/test_cli_test_runner.py tests/test_documentation_coverage.py
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```
