#!/usr/bin/env python3
"""Register the merge-conflict-detection hook for a project.

Usage:
    python scripts/register-merge-conflict-hook.py <project_id> <repo_workspace_path>

Example:
    python scripts/register-merge-conflict-hook.py agent-queue /home/jkern/agent-queue-workspaces/agent-queue

This creates a periodic hook that:
1. Runs check-merge-conflicts.sh against the project repository
2. If conflicts are found, asks the LLM to create resolution tasks
3. Runs every 30 minutes with a 30-minute cooldown
"""

import asyncio
import json
import os
import sys

# Add the src directory to the path so we can import project modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import Database
from models import Hook


async def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <project_id> <repo_workspace_path>")
        sys.exit(1)

    project_id = sys.argv[1]
    repo_path = sys.argv[2]

    # Resolve the path to check-merge-conflicts.sh relative to this script
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    check_script = os.path.join(scripts_dir, "check-merge-conflicts.sh")

    hook_id = "merge-conflict-detection"
    hook_name = "Merge Conflict Detection"

    trigger = {
        "type": "periodic",
        "interval_seconds": 1800,  # 30 minutes
    }

    context_steps = [
        {
            "type": "shell",
            "command": f"bash {check_script} {repo_path}",
            "timeout": 120,
            # If exit code is 0, no conflicts found — skip LLM invocation
            "skip_llm_if_exit_zero": True,
        },
    ]

    prompt_template = """You are a merge conflict detection assistant for the "{project_id}" project.

A periodic check has detected merge conflicts between task branches and the main branch.

## Conflict Report
```json
{{{{step_0}}}}
```

## Your Instructions

For EACH branch with a conflict:

1. **Create a resolution task** using the `create_task` tool with:
   - `project_id`: "{project_id}"
   - `title`: "Resolve merge conflict: <branch_name>"
   - `description`: Include the following details:
     - Branch name
     - Task ID (extracted from the branch name, which follows the pattern `task-id/description`)
     - Which files have conflicts
     - How many commits behind main the branch is
     - Instructions: "Rebase the branch onto main and resolve all merge conflicts. Ensure all tests pass after resolution."
   - `task_type`: "chore"
   - `priority`: 50 (higher priority since conflicts block merging)

2. **Post a notification** to the project's Discord channel mentioning:
   - How many branches have conflicts
   - Which branches are affected
   - That resolution tasks have been created

Be concise. Only create tasks for branches that actually have conflicts.
If a conflict resolution task likely already exists for a branch (based on the task ID), mention that in your response but still create the task — duplicates can be handled manually.
""".format(project_id=project_id)

    # Connect to the database
    db_path = os.environ.get(
        "AGENT_QUEUE_DB",
        os.path.join(os.path.dirname(scripts_dir), "data", "agent_queue.db"),
    )
    db = Database(db_path)
    await db.initialize()

    # Check if the project exists
    project = await db.get_project(project_id)
    if not project:
        print(f"Error: Project '{project_id}' not found in database at {db_path}")
        print("Available projects:")
        # List projects if possible
        sys.exit(1)

    # Check if hook already exists
    existing = await db.get_hook(hook_id)
    if existing:
        print(f"Hook '{hook_id}' already exists. Updating...")
        await db.update_hook(
            hook_id,
            trigger=json.dumps(trigger),
            context_steps=json.dumps(context_steps),
            prompt_template=prompt_template,
        )
        print(f"Updated hook '{hook_id}' for project '{project_id}'")
    else:
        hook = Hook(
            id=hook_id,
            project_id=project_id,
            name=hook_name,
            trigger=json.dumps(trigger),
            context_steps=json.dumps(context_steps),
            prompt_template=prompt_template,
            cooldown_seconds=1800,  # 30 minutes
        )
        await db.create_hook(hook)
        print(f"Created hook '{hook_id}' for project '{project_id}'")

    print("\nHook Details:")
    print(f"  ID:       {hook_id}")
    print(f"  Name:     {hook_name}")
    print("  Trigger:  Periodic every 30 minutes")
    print("  Cooldown: 30 minutes")
    print(f"  Repo:     {repo_path}")
    print(f"  Script:   {check_script}")


if __name__ == "__main__":
    asyncio.run(main())
