# Builds and releases

What AQ builds, how it is versioned, and what "shipping" means for this project
today.

## Why this page exists

The short version, stated up front so nobody goes looking for machinery that
does not exist: **Agent Queue has no release process.** It is not published to
PyPI or npm, there is no release workflow, no changelog file, and no semantic
version tag. It is installed from a checkout, in editable mode, and updated by
pulling `main`.

That is a deliberate consequence of how the project is used — the daemon runs
from a working checkout on the machine that develops it — but it is easy to
mistake for something missing, so this page records what exists instead.

## Vocabulary

* **Build backend** — the tool that turns the source tree into an installable
  distribution. AQ uses `setuptools`.
* **Editable install** — `pip install -e`, which puts the checkout itself on
  `sys.path` rather than a copy.
* **Workspace build** — the npm build of the dashboard and its client.
* **Checkpoint tag** — a Git tag marking a point in the project's history, not
  a released version.

## What is built

| Artefact | Built by | Output | Published? |
|---|---|---|---|
| The Python package | `setuptools>=83`, declared in [`pyproject.toml`](../../pyproject.toml) | An editable install providing `agent-queue`, `aq` and `agent-queue-mcp` entry points | No |
| The generated Python API client | `openapi-python-client`, pinned | `packages/aq-client/`, installed editable | No |
| The dashboard | `tsc -b && vite build` | `dashboard/dist/`, gitignored | No |
| The TypeScript client | `@hey-api/openapi-ts` | `packages/aq-ts-client/src/`, gitignored, consumed as source | No |

```bash
npm run build
```

runs the TypeScript client's build script (a no-op — it is consumed as source)
and then the dashboard's real build. The daemon serves the built dashboard when
one exists; during development you run `npm run dev` instead and let Vite serve
it.

> **Note.** `pyproject.toml` declares an `agent-queue-mcp` entry point pointing
> at `packages.mcp_server.mcp_server:main`, which is not in the tree — the MCP
> server is embedded in the daemon
> ([`src/embedded_mcp.py`](../../src/embedded_mcp.py)). Installing the package
> creates the script, but running it fails. Use the embedded server.

## Versioning

`version = "0.1.0"` in [`pyproject.toml`](../../pyproject.toml) has not moved
and is not used to gate anything. Nothing reads it at runtime to make a
decision, and no artefact is stamped with it.

The repository carries eight Git tags — `plan1-complete`, `plan2-complete`,
`plan3-complete`, `e2e-kit-complete`, `pre-agent-merge`, `pre-mass-merge`,
`pre-sdk-strip`, `pre-worktree-migration`. Every one of them is a **checkpoint
before or after a large migration**, kept so the state on either side can be
recovered. None is a release. Do not add a tag expecting tooling to notice it;
nothing does.

```bash
git tag
```

```text
e2e-kit-complete
plan1-complete
plan2-complete
plan3-complete
pre-agent-merge
pre-mass-merge
pre-sdk-strip
pre-worktree-migration
```

## Dependency pinning

`pyproject.toml`'s dependency list is unusually long and unusually commented,
and both are intentional. Most entries beyond the handful AQ actually imports
are **security pins on transitive dependencies**, each carrying the advisory it
addresses in a comment above it: `aiohttp` via discord.py, `cryptography` and
`pyjwt` via google-auth, `pillow` via mkdocs-material, and so on.

Two rules follow:

* **Do not remove a pin because "nothing imports it".** The comment says why it
  is there. If the advisory no longer applies, say so in the commit.
* **Keep the exact pins exact.** `openapi-python-client==` is the one pin that
  must match a second place — `GENERATOR_VERSION` in
  [`scripts/regenerate-api-client.sh`](../../scripts/regenerate-api-client.sh)
  — and a test fails when they drift
  ([code generation](codegen.md#the-python-api-client)).

To see what has moved:

```bash
python scripts/check-outdated-deps.py
```

That wrapper exists because `pip list --outdated` crashes on system packages
with non-PEP-440 versions; see [scripts](scripts.md#supported-diagnostics-and-one-off-tooling).

Frontend dependencies are pinned by [`package-lock.json`](../../package-lock.json)
at the repository root, which covers both npm workspaces.

## Upgrading an installation

Because there is no release, "upgrading" is pulling and reinstalling:

```bash
git pull
pip install -e ".[dev,cli]"
pip install -e packages/aq-client
npm install
```

If the schema moved, the **operator** — never a worker, never from inside a
worktree slot — applies migrations:

```bash
aq db current     # read-only: am I behind?
```

```bash
aq db upgrade     # operator only, outside a slot
```

See [migrations](../guides/migrations.md) for the guard that enforces this and
what to do when it fires.

## Inputs and outputs

| Input | Output | Where |
|---|---|---|
| The checkout + `pip install -e` | `agent-queue`, `aq`, entry points | your virtualenv |
| `openapi.json` + the pinned generator | `packages/aq-client/` | tracked in Git |
| `openapi.json` + `@hey-api/openapi-ts` | `packages/aq-ts-client/src/` | gitignored |
| `dashboard/src/` + Vite | `dashboard/dist/` | gitignored |

## State ownership

Nothing in the build path writes outside the checkout and the virtualenv. The
one place a build interacts with durable state is the database schema, and that
is owned by the operator's daemon, not by any build step.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `agent-queue-mcp: No module named 'packages.mcp_server'` | A stale entry point; the MCP server is embedded. | Use the daemon's embedded server. |
| `npm run build` cannot resolve `@aq/ts-client` | The generated client is missing. | `./scripts/regenerate-ts-client.sh --from-file` |
| A CVE scanner flags a transitive package | The pin may be out of date. | Update the pin *and* its comment in `pyproject.toml`. |
| `schema behind code; ask the operator to upgrade` | The install moved ahead of the database. | The operator runs `aq db upgrade` outside a worktree slot. |
| `openapi-python-client` version mismatch after a `pip install -e '.[dev]'` | The `dev` extra pin changed. | Match `GENERATOR_VERSION` and regenerate in one commit. |

## Related pages

* [Setup](setup.md) — the install this page is the maintenance half of.
* [Code generation](codegen.md) — the pinned generator and its guards.
* [CI](ci.md#what-ci-does-not-do) — confirmation that nothing publishes.
* [Migrations](../guides/migrations.md) — the only durable state a build touches.
* [Pull requests and delivery](pull-requests.md) — how `main` moves at all.

## Source and tests

[`pyproject.toml`](../../pyproject.toml),
[`package.json`](../../package.json),
[`scripts/check-outdated-deps.py`](../../scripts/check-outdated-deps.py),
[`scripts/regenerate-api-client.sh`](../../scripts/regenerate-api-client.sh).

```bash
aq test tests/test_api_client_contract.py tests/test_cli_module_entry.py
```
