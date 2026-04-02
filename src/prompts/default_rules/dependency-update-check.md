# Dependency Update Check

## Intent
Periodically check for outdated or vulnerable dependencies and create
tasks to update them when appropriate.

## Trigger
Check every 24 hours.

## Logic
1. Run dependency audit commands (pip audit, npm audit, etc.) appropriate for the project
2. Check for outdated packages with known security vulnerabilities
3. If critical vulnerabilities are found, create a high-priority task to update them
4. For non-critical updates, collect them into a summary note
5. Skip if no actionable updates are found
