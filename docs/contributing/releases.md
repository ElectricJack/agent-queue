# Builds and releases

What AQ builds, how it is versioned, and what "shipping" means for this project
today.

## Why this page exists

AQ release artifacts are Python wheels. They contain the runtime, `aq` and
`agent-queue` console scripts, required package resources, and a pre-built
dashboard at `/dashboard`; an installed user neither builds the frontend nor
keeps a development checkout. A source checkout remains the contributor path.

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
| The Python package | `setuptools>=83`, declared in [`pyproject.toml`](../../pyproject.toml) | A wheel providing `agent-queue` and `aq` | Yes, release artifact |
| The generated Python API client | `openapi-python-client`, pinned | `packages/aq-client/`, installed editable | No |
| The dashboard | `scripts/build_release_artifact.py` then `python -m build` | Verified package data served at `/dashboard` | Yes, inside the wheel |
| The TypeScript client | `@hey-api/openapi-ts` | `packages/aq-ts-client/src/`, gitignored, consumed as source | No |

```bash
npm run build
```

runs the TypeScript client's build script (a no-op — it is consumed as source)
and then the dashboard's real development build. Release builds use the staging
command below; development continues to use `npm run dev` and Vite.

## Versioning, integrity, and updates

`project.version` in [`pyproject.toml`](../../pyproject.toml) is the release
selector. Publish immutable wheels, install an explicit version such as
`agent-queue==0.1.0`, and confirm it with `aq --version`. The dashboard
manifest embedded in that wheel records the same version and a SHA-256 digest
for every served asset; the daemon refuses to mount a missing or altered bundle.

Publish a SHA-256 requirements lock beside each release and install it with
pip's `--require-hashes` option. The package-data manifest protects the bundle
after installation; pip's hash checking protects the downloaded wheel.

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
`pyjwt` via google-auth, `pillow` via the imaging stack, and so on.

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

## Building and upgrading an installation

Build a release wheel only from a clean release checkout:

```bash
python scripts/build_release_artifact.py
python -m build --wheel
```

The first command runs the frontend with its production `/dashboard/` base and
stages it in `src/dashboard_assets/dist/`; this is release-only generated
output. The second command must run after staging so the wheel includes the
manifest and assets. Users update by selecting another published immutable
version and its matching hash lock, for example `pip install --upgrade
--require-hashes -r requirements-aq-0.1.1.txt`. Do not use `git pull` as an
update mechanism for a wheel installation.

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
| A selected, hash-verified wheel | `agent-queue`, `aq`, built dashboard | your virtualenv |
| `openapi.json` + the pinned generator | `packages/aq-client/` | tracked in Git |
| `openapi.json` + `@hey-api/openapi-ts` | `packages/aq-ts-client/src/` | gitignored |
| `dashboard/src/` + Vite | `src/dashboard_assets/dist/` + manifest | release staging, then wheel |

## State ownership

Nothing in the build path writes outside the checkout and the virtualenv. The
one place a build interacts with durable state is the database schema, and that
is owned by the operator's daemon, not by any build step.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| Dashboard missing from an installed wheel | Release staging was skipped or assets were altered. | Rebuild after `python scripts/build_release_artifact.py`; verify its SHA-256 lock. |
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
