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
| setuptools | 74.1.3 | 82.0.1 | PYSEC-2025-49 |

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
