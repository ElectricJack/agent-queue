---
tags: [spec, packaging, build-system, dependencies]
---

# Packaging and Build System Specification

## 1. Overview

This specification documents the packaging, build-system, and dependency-management
setup for Agent Queue. The project is distributed as a standards-compliant Python
package defined by `pyproject.toml` at the repository root. A second `pyproject.toml`
exists under `packages/memsearch/` for the vendored memsearch fork (see
[memsearch](#5-nested-package-packagesmemsearch)).

The primary goals of this setup are:

- Reproducible installs across Python 3.12+ environments
- Explicit pinning of security-critical transitive dependencies (CVE mitigation)
- Clear grouping of optional feature sets via extras
- A single source of truth for runtime, dev, and documentation dependencies

## Source Files

- `pyproject.toml` (root)
- `packages/memsearch/pyproject.toml` (vendored sub-package)
- `uv.lock` (optional — generated lockfile when `uv` is used; not required for
  development installs)

---

## 2. Build System

Agent Queue uses [setuptools](https://setuptools.pypa.io/) as its PEP 517 build
backend. The build-system table at the top of `pyproject.toml` pins the minimum
versions of both `setuptools` and `wheel`:

```toml
[build-system]
requires = ["setuptools>=78.1.1", "wheel>=0.38.1"]
build-backend = "setuptools.build_meta"
```

### 2.1 Why these pins?

Both pins are **security minimums**, not feature minimums:

| Package      | Minimum   | Rationale                                                                                              |
|--------------|-----------|--------------------------------------------------------------------------------------------------------|
| `setuptools` | `>=78.1.1` | Addresses **PYSEC-2025-49 / CVE-2025-47273** — a path-traversal in `PackageIndex._download_url` that allowed writes to arbitrary filesystem locations (potential RCE). |
| `wheel`      | `>=0.38.1` | Addresses **CVE-2022-40898** — a ReDoS in the wheel CLI. |

Both values are mirrored in the runtime `dependencies` list (§3.2) so that
installed environments — not just build environments — receive the patched
versions.

### 2.2 Backend

`setuptools.build_meta` is used directly (not the legacy `build_meta:__legacy__`
shim). Package discovery is handled by:

```toml
[tool.setuptools.packages.find]
where = ["."]
```

This instructs setuptools to scan from the repo root. The `src/` directory is
laid out as a regular Python package (no `src-layout` remapping), and `packages/`
contains sibling packages that are installed separately (see §5).

---

## 3. Project Metadata

### 3.1 Core metadata

```toml
[project]
name = "agent-queue"
version = "0.1.0"
requires-python = ">=3.12"
```

- **Python version:** 3.12+ is required. The project uses `ruff` with
  `target-version = "py312"` and may assume 3.12 language features (e.g.,
  improved generic syntax, modern typing). Python 3.13 is also supported.
- **Version:** Currently `0.1.0`. The project has not yet cut a release and the
  version is not managed through `setuptools_scm` or similar — it is edited
  manually when bumping.

### 3.2 Runtime dependencies

The `[project].dependencies` list captures all packages required for a minimal
daemon install. Dependencies fall into four broad groups:

**Core infrastructure**
- `aiosqlite>=0.20.0` — async SQLite driver
- `sqlalchemy[asyncio]>=2.0` — ORM / Core with async support
- `alembic>=1.13` — schema migrations
- `discord.py>=2.5.2,<2.6` — Discord bot framework. **Upper bound is
  intentional:** 2.6+ imports the Python 3.14-only `annotationlib` stdlib module
  via `discord.ext.commands.flags`, which breaks on 3.12/3.13. The cap will be
  lifted when `requires-python` rises to 3.14.
- `claude_agent_sdk>=0.1.55` — Claude SDK for agent invocation

**API + web**
- `fastapi>=0.135.0`, `starlette>=0.27.0`, `pydantic>=2.0`

**UX + logging**
- `PyYAML>=6.0`, `structlog>=25.1.0`, `rich>=13.9`, `pygments>=2.20.0`

**Security-driven transitive pins** — these packages are pulled in transitively
by other dependencies; they are explicitly pinned in `dependencies` so that the
patched version is always installed, regardless of what the upstream resolves to.

| Package            | Minimum      | CVE / Advisory                                   | Arrives via                                   |
|--------------------|--------------|--------------------------------------------------|-----------------------------------------------|
| `zipp`             | `>=3.19.1`   | CVE-2024-5569 (DoS via crafted zip, infinite loop in `zipp.Path`) | `importlib-metadata` |
| `cryptography`     | `>=46.0.7`   | CVE-2026-39892 (buffer overflows in `Hash.update()` with non-contiguous buffers) | `google-auth` (inbox extras) |
| `python-multipart` | `>=0.0.26`   | CVE-2026-40347 (DoS via crafted multipart/form-data preamble/epilogue) | `fastapi`/`starlette` form parsing |
| `oauthlib`         | `>=3.2.1`    | PYSEC-2022-269 / CVE-2022-36087 (DoS via malicious redirect URIs in `uri_validate`) | `google-auth-oauthlib`, `requests-oauthlib` (inbox extras) |
| `setuptools`       | `>=78.1.1`   | PYSEC-2025-49 / CVE-2025-47273 (path traversal in `PackageIndex._download_url`) | transitive (notably via `memsearch`) |

**Rule:** when adding a new CVE-driven pin, always include a comment in
`pyproject.toml` naming the advisory, summarising the issue, and describing how
the package is pulled in. The comment blocks above the existing pins are the
canonical template.

Audit history for these pins lives in `notes/dependency-audit-YYYY-MM-DD.md`
files. Each audit note records the `pip-audit` findings that motivated the
change and confirms the spec-divergence outcome (see §6).

### 3.3 Optional dependencies (extras)

Extras group features that aren't needed for the minimal install:

| Extra        | Purpose                                                                 |
|--------------|-------------------------------------------------------------------------|
| `anthropic`  | Claude via the Anthropic SDK (`anthropic>=0.42.0`)                      |
| `gemini`     | Google Gemini via `google-genai>=1.0.0`                                 |
| `inbox`      | Gmail/OAuth helpers (`google-api-python-client`, `google-auth-oauthlib`) |
| `postgresql` | Postgres backend driver (`asyncpg>=0.29.0`)                             |
| `ollama`     | Local models via the `openai>=1.0.0` client                             |
| `telegram`   | Telegram bot support (`python-telegram-bot[ext]>=20.0`)                 |
| `mcp`        | MCP server dependencies (`mcp>=1.0.0`)                                  |
| `cli`        | `aq` CLI (`click`, `prompt-toolkit`, `agent-queue-api-client`)          |
| `memory`     | Semantic memory via the vendored memsearch fork                         |
| `docs`       | `mkdocs` + `mkdocs-material` + `mkdocstrings[python]`                   |
| `dev`        | Test + lint tools — see §3.4                                            |

Install examples:

```bash
pip install -e ".[dev,cli]"            # the typical development install
pip install -e ".[dev,cli,memory]"     # add semantic memory (installs memsearch)
pip install -e ".[anthropic,postgresql]"  # production-style install
```

### 3.4 Dev dependencies

The `dev` extra pins tools used for testing, linting, and pre-commit:

```toml
dev = [
    # CVE-2025-71176: pytest <9.0.3 uses predictable /tmp/pytest-of-{user}
    # directory names, allowing local DoS or possible privilege escalation.
    "pytest>=9.0.3",
    "pytest-asyncio>=0.23",
    "pytest-xdist>=3.5",
    "ruff>=0.4.0",
    "pre-commit>=3.5.0",
    # CVE-2022-40898: wheel <0.38.1 has a ReDoS in the wheel CLI
    "wheel>=0.38.1",
]
```

- **`pytest>=9.0.3`** — pinned for CVE-2025-71176. This was the subject of the
  `grand-ridge` update task; future bumps should retain the minimum.
- **`pytest-xdist`** — enables `pytest -n auto` for ~5× faster test runs
  (see root `CLAUDE.md`).
- **`wheel>=0.38.1`** — duplicated from the build-system requires so that the
  patched wheel is present at runtime (some plugins, e.g. `aq-vibecop`, invoke
  the wheel CLI).
- **`ruff>=0.4.0`** — formatter + linter. Configuration lives in the `tool.ruff`
  tables (§4.2).

### 3.5 Console scripts

```toml
[project.scripts]
agent-queue = "src.main:main"
agent-queue-mcp = "packages.mcp_server.mcp_server:main"
aq = "src.cli.app:main"
```

Three entry points are registered:

- `agent-queue` — runs the daemon (orchestrator + Discord + MCP). Canonical
  invocation for production and `./run.sh start`.
- `agent-queue-mcp` — runs only the MCP server (used when the daemon is not
  already exposing MCP).
- `aq` — the CLI (`aq logs`, `aq plugin install`, etc.).

Plugin entry points use the `aq.plugins` entry-point group and are documented
in `plugin-system.md`.

---

## 4. Tool Configuration

`pyproject.toml` also houses configuration for project-level tools so that
settings live alongside the package metadata they apply to.

### 4.1 pytest

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests", "packages"]
markers = [
    "functional: tests that launch real Claude CLI (require auth + API key)",
    "functional_mcp: functional tests requiring npm/npx for MCP servers",
    "integration: tests requiring external dependencies (memsearch, Milvus Lite, etc.)",
]
```

- **`asyncio_mode = "auto"`** — async tests do not need an explicit
  `@pytest.mark.asyncio`; pytest-asyncio discovers them automatically. This is a
  hard requirement; tests rely on it.
- **`testpaths`** — both the top-level `tests/` tree and any nested package
  test suites under `packages/` are collected.
- **Markers** — the three defined markers gate tests that require external
  resources. They are enforced only when used; CI may skip or include them
  selectively.

### 4.2 ruff

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.format]
quote-style = "double"
```

- **Line length: 100.** This applies to both formatting and lint warnings.
- **Target: py312.** Ruff's `pyupgrade` and related rules assume Python 3.12+.
- **Quote style: double.** The formatter rewrites single quotes unless a double
  would require escaping.

No `[tool.ruff.lint.select]` / `ignore` blocks are configured at present — the
root project uses ruff's default rule set. The memsearch sub-package uses a
broader rule selection; see §5.2.

---

## 5. Nested Package: `packages/memsearch/`

The `memsearch` directory is a **vendored fork** of the upstream Zilliz
`memsearch` library. It is installed via the `memory` extra:

```toml
[project.optional-dependencies]
memory = [
    "memsearch @ file:packages/memsearch",
]
```

The direct-URL install (`@ file:...`) means `pip install -e ".[memory]"` picks
up the local sub-package rather than the PyPI release. This lets us iterate on
memsearch alongside agent-queue.

### 5.1 Its own build system

`packages/memsearch/pyproject.toml` uses a different backend:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Hatchling was chosen upstream. We retain the upstream build backend to minimise
fork drift.

### 5.2 Its own Python + ruff policy

- `requires-python = ">=3.10"` — broader than the root project, because the
  upstream memsearch supports Python 3.10–3.13.
- `target-version = "py310"`, `line-length = 120`, and an explicit
  `[tool.ruff.lint.select]` list (E/W/F/I/UP/B/C4/SIM/PIE/PGH/RUF/T20/PERF) —
  also inherited from upstream.

When modifying code under `packages/memsearch/`, follow memsearch's own
conventions, not the root project's. See `packages/memsearch/CLAUDE.md` for
memsearch-specific build, test, and release procedures.

### 5.3 Dependency groups

memsearch uses PEP 735 `[dependency-groups]` (e.g., `dev`, `docs`) rather than
`optional-dependencies` for developer tooling. This is a quirk of the upstream
fork and does not apply to agent-queue itself.

---

## 6. Dependency Management Workflow

### 6.1 Tooling

- **`uv`** is the preferred resolver / installer. When used, it produces a
  `uv.lock` that pins exact transitive versions for reproducible environments.
  `pip install -e ".[dev,cli]"` also works and is sufficient for development.
- **`pip-audit`** is used to scan the installed environment for known CVEs.
  Reports are written as JSON and stored under `notes/` with the audit.
- **`scripts/check-outdated-deps.py`** identifies outdated packages and emits a
  JSON report when invoked with `--json`.

### 6.2 CVE response procedure

1. Run `pip-audit` (or receive a scheduled `vibecop` scan).
2. For each vulnerability with a fix version available:
   - Update the constraint in `pyproject.toml`. Transitive packages move into
     the top-level `dependencies` list with a bounding `>=` pin and a comment
     block naming the CVE.
   - If `uv` is in use, run `uv lock` to regenerate `uv.lock`.
   - Install the environment and run `pytest tests/ -n auto`.
3. Create or update `notes/dependency-audit-YYYY-MM-DD.md` with:
   - The packages changed, the CVEs addressed, and the reasoning.
   - A spec-divergence check result (see §6.3).
4. Commit under a dedicated branch. Serialise dependency-update tasks — parallel
   updates contend for the same workspace lease on `pyproject.toml` (and
   `uv.lock`, when present) and can exhaust task retries.

### 6.3 Spec divergence check

After any change to `pyproject.toml`, confirm that the spec stays in sync:

- If a build-system requirement changes (e.g., a new `setuptools` minimum), this
  document (`docs/specs/packaging.md`) must be updated.
- If a new CVE-driven runtime pin is added, the table in §3.2 must list it.
- If a new extra is added, §3.3 must list it.
- If an entry point is added, §3.5 must list it.
- If `tool.pytest` or `tool.ruff` options change, §4 must reflect them.

The `task-outcome` playbook automates this check: after a dependency-update task
completes, it scans for spec divergence and creates a follow-up task if any is
found.

---

## 7. Non-Goals

- **Wheel distribution.** Agent Queue is not yet published to PyPI. `wheel` is
  still required by the build system (it is a PEP 517 dependency of setuptools
  for bdist builds) and by some plugins, but no release workflow publishes
  wheels for agent-queue itself.
- **Version bumping automation.** There is no `setuptools_scm`, `bumpversion`,
  or release script. The `[project].version` field is edited by hand.
- **Lockfile-free installs.** `pip install -e .` without `uv` works for
  development. Production installs should use a lockfile (`uv.lock` generated
  by `uv`) for reproducibility.

---

## 8. Related Specifications

- [`plugin-system.md`](plugin-system.md) — plugin `pyproject.toml` conventions,
  the `aq.plugins` entry-point group, and plugin packaging layout.
- [`mcp-server.md`](mcp-server.md) — MCP server entry point registration.
- [`config.md`](config.md) — runtime YAML configuration (distinct from package
  metadata).
- [`design/guiding-design-principles.md`](design/guiding-design-principles.md) —
  "human-readable files are the source of truth" applies to `pyproject.toml` as
  much as to vault markdown.
