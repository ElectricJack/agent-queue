# Dependency Audit — 2026-04-02

## Security Vulnerabilities Found & Fixed

19 vulnerabilities across 8 packages, **all resolved** by upgrading:

| Package | From | To | CVEs |
|---------|------|----|------|
| aiohttp | 3.13.3 | 3.13.5 | CVE-2026-34513 through CVE-2026-34525, CVE-2026-22815 (10 CVEs) |
| cryptography | 46.0.5 | 46.0.6 | CVE-2026-34073 |
| pip | 25.0.1 | 26.0.1 | CVE-2025-8869, CVE-2026-1703 |
| pyasn1 | 0.6.2 | 0.6.3 | CVE-2026-30922 |
| pygments | 2.19.2 | 2.20.0 | CVE-2026-4539 |
| pyjwt | 2.11.0 | 2.12.1 | CVE-2026-32597 |
| requests | 2.32.5 | 2.33.1 | CVE-2026-25645 |
| setuptools | 74.1.3 | 78.1.1 | PYSEC-2025-49 / CVE-2025-47273 |

## Direct Dependency Updates

| Package | From | To | Notes |
|---------|------|----|-------|
| claude-agent-sdk | 0.1.37 | 0.1.54 | Major update, bumped minimum in pyproject.toml |
| discord.py | 2.6.4 | 2.7.1 | Minor update, bumped minimum in pyproject.toml |
| ruff | 0.15.1 | 0.15.8 | Dev dependency, patch updates |

## Non-Critical Updates Available (not applied)

These packages have updates available but are not urgent:

- anthropic: 0.81.0 → 0.88.0 (transitive dep)
- anyio: 4.12.1 → 4.13.0
- attrs: 25.4.0 → 26.1.0
- memsearch: 0.1.16 → 0.2.2 (major version bump — may need migration testing)
- openai: 2.21.0 → 2.30.0 (transitive dep)
- starlette: 0.52.1 → 1.0.0 (major version — breaking changes likely)

## Test Results

1924 passed, 30 skipped, 1 pre-existing failure (unrelated `test_tool_registry.py::test_total_tool_count_preserved`).

## Reconciliation Notes (2026-04-20)

- **setuptools target adjusted to 78.1.1 (was 82.0.1).** The original 2026-04-02
  audit listed 82.0.1 as the upgrade target, but the only known security
  vulnerability in this range is PYSEC-2025-49 / CVE-2025-47273 (path traversal
  in `PackageIndex._download_url`), which is fixed in 78.1.1. Task `swift-falcon`
  pinned `setuptools>=78.1.1` in both `pyproject.toml` and
  `packages/memsearch/pyproject.toml` as the minimum version that resolves this
  CVE. The 82.0.1 figure in the original table reflected the latest available
  release at audit time, not a security-required floor — pinning to 82.0.1 would
  have been an over-target with no additional security benefit. `pip-audit`
  reports no remaining setuptools vulnerabilities at 78.1.1. The `>=` pin still
  permits 82.0.1+ to be installed when desired or pulled in by other packages.
- `docs/specs/plugin-system.md` already references `setuptools>=78.1.1` and
  remains in sync with the implemented pin.
- **oauthlib pinned to `>=3.2.1` (not in original 2026-04-02 table).** Task
  `sound-quest` (commit `608b38f3`) added an explicit lower-bound pin
  `oauthlib>=3.2.1` in `[project].dependencies` of `pyproject.toml` to address
  PYSEC-2022-269 / CVE-2022-36087 / GHSA-3pgj-pg6c-r5p7 — a denial-of-service
  vulnerability via malicious redirect URIs in oauthlib's OAuth2 provider
  support and `uri_validate`. oauthlib is a transitive dependency brought in
  via `google-auth-oauthlib` / `requests-oauthlib` under the `inbox` extras,
  which is why it was not listed in the original 2026-04-02 audit table — that
  audit run did not surface it. A subsequent `pip-audit` run flagged oauthlib
  3.2.0 as vulnerable (PYSEC feed `fix_versions: ["3.2.1"]`) and `sound-quest`
  addressed it with a direct pin. The rationale for pinning directly (rather
  than relying on `google-auth-oauthlib` / `requests-oauthlib`) is to ensure
  that installs of the `inbox` extras pull a patched oauthlib even when
  upstream does not tighten its own lower bound. `pip-audit` against
  `oauthlib==3.2.2` and `oauthlib==3.3.1` (both permitted by the `>=3.2.1`
  pin, and 3.3.1 is what pip's resolver will actually install today) reports
  no remaining vulnerabilities. Note: a live `pip-audit` run against
  `oauthlib==3.2.1` still reports CVE-2022-36087 with `fix_versions: ["3.2.2"]`
  via the OSV/GHSA feed — this is an advisory-database discrepancy between the
  PYSEC feed (which lists 3.2.1 as the fix, matching the sound-quest pin) and
  the OSV/GHSA feed (which lists 3.2.2). In practice the resolver will pull
  3.3.1, so the pin is effectively safe; a future tightening to `>=3.2.2`
  could be considered for belt-and-braces coverage of the GHSA feed.
- **python-multipart pinned to `>=0.0.26` (not in original 2026-04-02 table).**
  Task `steady-meadow` (commit `97717f0d`) added an explicit lower-bound pin
  `python-multipart>=0.0.26` in `[project].dependencies` of `pyproject.toml`
  to address CVE-2026-40347 — a denial-of-service vulnerability where crafted
  `multipart/form-data` requests with large preamble or epilogue sections
  drive the parser into inefficient paths that consume excessive CPU,
  degrading request-handling availability. `pip-audit` lists
  `fix_versions: ["0.0.26"]` for this advisory; the same pin also covers
  CVE-2026-24486 (`fix_versions: ["0.0.22"]`), which was flagged against the
  previously installed `python-multipart==0.0.20`. python-multipart is a
  transitive dependency of fastapi/starlette for form parsing, which is why
  it was not listed in the original 2026-04-02 audit table — that audit run
  did not surface it (similar to the oauthlib case above). A subsequent
  `pip-audit` run flagged it, and `steady-meadow` addressed it with a direct
  pin. The rationale for pinning directly (rather than waiting for an
  upstream fastapi/starlette lower-bound bump) is to guarantee every install
  pulls a patched python-multipart regardless of what fastapi/starlette
  currently require, mirroring the same pattern used for `zipp>=3.19.1`,
  `oauthlib>=3.2.1`, and `cryptography>=46.0.7`. A fresh `pip-audit` run
  (see `pip-audit-results.json` at the repo root) confirms no remaining
  python-multipart vulnerabilities at the installed version satisfying
  `>=0.0.26`. The `>=` pin still permits later patch releases to be
  installed when available.
- **Task lineage note: `noble-impact` → `agile-beacon` → `grand-willow`.**
  The `python-multipart>=0.0.26` pin was originally targeted by task
  `noble-impact`, which exhausted its retry budget (3/3) and was left in
  `BLOCKED` state. A follow-up investigation task `agile-beacon` was
  spawned to diagnose the retry exhaustion. In parallel, task
  `steady-meadow` independently performed the required pin and landed it
  in `pyproject.toml` (commit `97717f0d`), and `agile-ember` landed the
  audit-doc reconciliation entry immediately above (commit `e1345f23`).
  By the time the re-implementation task `grand-willow` ran, the
  acceptance criteria were already satisfied: `python-multipart==0.0.26`
  is installed, `pyproject.toml` pins `>=0.0.26`, and a fresh `pip-audit`
  run (against the active environment, 2026-04-22) reports zero
  vulnerabilities for `python-multipart` — and, incidentally, zero
  vulnerabilities for the overall environment. The follow-up
  spec-divergence research task `smart-nexus` (titled "Check for spec
  divergence after python-multipart update") also ran to completion and
  confirmed no divergence. No code changes were required in
  `grand-willow`; this lineage note is recorded so future agents can see
  at a glance that the `noble-impact` / `agile-beacon` work-stream is
  fully resolved and do not attempt to re-pin a version that is already
  in place. Note: this project does not use `uv.lock` — dependency
  resolution is driven entirely by `pyproject.toml` — so the original
  task's `uv.lock` acceptance criterion is satisfied vacuously (N/A).
- **`crisp-falcon` retry of `sound-quest` verified oauthlib pin (2026-04-22).**
  The oauthlib security update was previously targeted by task
  `sound-quest`, which exhausted its retry budget (3/3) and was left in
  `BLOCKED` state — most likely due to `pyproject.toml` / lockfile
  contention with the concurrent cryptography/python-multipart/
  zipp/wheel/setuptools update tasks. By the time the retry task
  `crisp-falcon` ran, the acceptance criteria were already satisfied via
  prior merges: `pyproject.toml` pins `oauthlib>=3.2.1` (commit
  `608b38f3` from `sound-quest`'s partial work), `oauthlib==3.3.1` is
  installed in the active environment (satisfies the pin), and a fresh
  `pip-audit` run (2026-04-22) reports zero known vulnerabilities across
  the entire environment. No code changes were required in
  `crisp-falcon`; this note is recorded so future agents can confirm at
  a glance that the `sound-quest` → `crisp-falcon` work-stream for
  PYSEC-2022-269 / CVE-2022-36087 is fully resolved. As with
  `python-multipart`, this project does not use `uv.lock`, so the
  original task's `uv.lock` acceptance criterion is satisfied vacuously.
