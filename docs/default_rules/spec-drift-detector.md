# Spec Drift Detector

## Intent
Detect when code changes diverge from specifications, keeping documentation
and implementation in sync.

## Trigger
When a task is completed.

## Logic
1. Check if the completed task modified any source files that have corresponding specs
2. Compare the changed code behavior against the relevant spec sections
3. If discrepancies are found, create a task to update the spec
4. Focus on meaningful behavioral changes, not cosmetic code differences
5. Skip if no specs are affected or changes are spec-consistent
