## Sync Workspaces — {{project_id}}

    **Task Type:** Orchestrator-managed sync workflow (handled automatically by the orchestrator)

    ### Workflow Steps (executed by the orchestrator, NOT the agent):
    1. **Pause Project** — Prevent new tasks from being queued for `{{project_id}}`
    2. **Wait for Active Tasks** — Monitor all running tasks and wait for completion
    3. **Merge Feature Branches** — Launch a Claude Code agent to merge all feature work
    4. **Cleanup & Resume** — Unlock workspaces and resume the project

    ### Project Details:
    - **Project ID:** {{project_id}}
    - **Default Branch:** {{default_branch}}
    - **Workspace Root:** {{workspace_root}}

    ### Workspaces:
    {{workspace_paths}}

    ### Merge Strategy:
    - Merge and push one workspace at a time
    - Pull updates before working on subsequent workspaces
    - Preserve ALL feature work unless it's duplicated or made irrelevant by subsequent changes
    - Resolve conflicts intelligently, preferring to preserve feature functionality
    - Ensure each workspace ends up on `{{default_branch}}` with no remaining feature branches from agent-queue

    ### Why This Exists:
    This synchronizes workspaces that have drifted from the default branch, consolidating
    feature work stuck on feature branches across multiple workspaces.
    