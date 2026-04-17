---
id: dependency-audit
triggers:
  - timer.24h
scope: system
---

# Dependency Audit

Run dependency audit (pip-audit + check-outdated-deps). Create
high-priority tasks for critical vulnerabilities. Summarize
non-critical updates as a note.

First, run `python scripts/check-outdated-deps.py --json` from the
workspace root to get outdated packages. This handles system
packages with non-PEP 440 versions that crash pip directly.

Then, run `pip-audit --format=json` to check for packages with
known security vulnerabilities.

If critical vulnerabilities are found, create a high-priority task
to update the affected packages immediately.

For non-critical updates (minor version bumps, non-security
outdated packages), collect them into a summary note. Do not
create individual tasks for each — a single note is sufficient.

Skip if no actionable updates or vulnerabilities are found.
