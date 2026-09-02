# Periodic Project Review

## Intent
Regularly review project health and catch issues before they become problems.

## Trigger
Check every 30 minutes.

## Logic
1. Check for stuck tasks (tasks in ASSIGNED or IN_PROGRESS for too long)
2. Check for orphaned hooks (hooks referencing deleted projects)
3. Verify rule-hook synchronization
4. Look for BLOCKED tasks with no resolution path
5. Post a summary if any issues are found
