# Post-Action Reflection

## Intent
After each significant action (task completion, hook execution, user request),
evaluate the result and determine if follow-up work is needed.

## Trigger
When a task is completed.

## Logic
1. Review the completed task's summary and output
2. Check if the result matches the task's acceptance criteria
3. Evaluate any project-specific rules that might apply
4. Determine if follow-up tasks are needed
5. Record any insights learned (project notes or — if the memory plugin is installed — `memory_store`)
