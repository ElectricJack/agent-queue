# Dependency Update Check

<!-- aq:historical -->
> **Historical sample.** Illustrative content that nothing installs or loads. It
> was written for an earlier runtime, so copying it will not reproduce the
> behaviour it describes. Start at [the documentation home](../README.md); see
> [historical material](../history/README.md).

## Intent
Periodically check for outdated or vulnerable dependencies and create
tasks to update them when appropriate.

## Trigger
Check every 24 hours.

## Logic
1. Run `python scripts/check-outdated-deps.py --json` from the workspace root to get outdated packages (this handles system packages with non-PEP 440 versions that crash `pip list --outdated` directly)
2. Run `pip-audit --format=json` to check for packages with known security vulnerabilities
3. If critical vulnerabilities are found, create a high-priority task to update them
4. For non-critical updates, collect them into a summary note
5. Skip if no actionable updates are found
