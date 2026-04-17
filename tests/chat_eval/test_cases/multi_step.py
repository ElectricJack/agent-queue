"""Multi-turn conversation test cases for the Supervisor (context carry-over).

Each TestCase contains multiple Turns that build on each other, testing the
Supervisor's ability to carry forward context (project names, task IDs, etc.)
across turns — essential for the supervisor-based workflow.

20 test cases: verified against current supervisor-based architecture.

Updated: supervisor refactor review — added supervisor-specific multi-step tests
for task creation→approval and rule creation workflows.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- Create project then add task ---
    TestCase(
        id="multi-create-project-then-task",
        description="Create a project, then add a task to it in the next turn",
        turns=[
            Turn(
                user_message="create a project called Backend API",
                expected_tools=[
                    ExpectedTool(name="create_project", args={"name": "Backend API"}),
                ],
            ),
            Turn(
                user_message="now add a task to it: implement auth",
                expected_tools=[
                    ExpectedTool(name="create_task"),
                ],
            ),
        ],
        category="multi_step",
        tags=["project", "task", "creation", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- View task then approve ---
    TestCase(
        id="multi-view-then-approve",
        description="View a task, then approve it based on the previous context",
        turns=[
            Turn(
                user_message="show me task t-1",
                expected_tools=[
                    ExpectedTool(name="get_task", args={"task_id": "t-1"}),
                ],
            ),
            Turn(
                user_message="now approve it",
                expected_tools=[
                    ExpectedTool(name="approve_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
        category="multi_step",
        tags=["task", "approval", "context-carry"],
        difficulty=Difficulty.MEDIUM,
        setup_commands=[
            ("create_project", {"name": "Test", "project_id": "p-test"}),
            (
                "create_task",
                {
                    "project_id": "p-test",
                    "title": "review auth module",
                    "task_id": "t-1",
                },
            ),
        ],
    ),
    # --- List projects then pause one ---
    TestCase(
        id="multi-list-then-pause-project",
        description="List projects, then pause 'the first one' referencing list output",
        turns=[
            Turn(
                user_message="list projects",
                expected_tools=[ExpectedTool(name="list_projects")],
            ),
            Turn(
                user_message="pause the first one",
                expected_tools=[ExpectedTool(name="pause_project")],
            ),
        ],
        category="multi_step",
        tags=["project", "list", "pause", "context-carry"],
        difficulty=Difficulty.HARD,
        setup_commands=[
            ("create_project", {"name": "Alpha", "project_id": "p-alpha"}),
            ("create_project", {"name": "Beta", "project_id": "p-beta"}),
        ],
    ),
    # --- Git workflow: branch, commit, push ---
    TestCase(
        id="multi-git-branch-commit-push",
        description="Full git workflow: create branch, commit, then push",
        turns=[
            Turn(
                user_message="create a branch called feature/login",
                expected_tools=[ExpectedTool(name="git_create_branch")],
            ),
            Turn(
                user_message="commit these changes with message 'add login form'",
                expected_tools=[ExpectedTool(name="git_commit")],
            ),
            Turn(
                user_message="now push",
                expected_tools=[ExpectedTool(name="git_push")],
            ),
        ],
        category="multi_step",
        tags=["git", "branch", "commit", "push", "workflow"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- List agents then delete one ---
    TestCase(
        id="multi-list-agents-then-delete",
        description="Check agents, then delete one by referring to it from the listing",
        turns=[
            Turn(
                user_message="check what agents we have",
                expected_tools=[ExpectedTool(name="list_agents")],
            ),
            Turn(
                user_message="delete the second one",
                expected_tools=[ExpectedTool(name="delete_agent")],
            ),
        ],
        category="multi_step",
        tags=["agent", "list", "delete", "context-carry"],
        difficulty=Difficulty.HARD,
        setup_commands=[
            ("create_agent", {"name": "agent-1"}),
            ("create_agent", {"name": "agent-2"}),
        ],
    ),
    # --- View task then edit it ---
    TestCase(
        id="multi-view-then-edit-task",
        description="View a task, then edit its title based on the viewed task",
        turns=[
            Turn(
                user_message="show me task t-5",
                expected_tools=[
                    ExpectedTool(name="get_task", args={"task_id": "t-5"}),
                ],
            ),
            Turn(
                user_message="change its title to 'refactor database layer'",
                expected_tools=[
                    ExpectedTool(name="edit_task"),
                ],
            ),
        ],
        category="multi_step",
        tags=["task", "view", "edit", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Get task result then view diff ---
    TestCase(
        id="multi-result-then-diff",
        description="Get task result, then ask to see the diff for the same task",
        turns=[
            Turn(
                user_message="what was the result of task t-3?",
                expected_tools=[
                    ExpectedTool(name="get_task_result", args={"task_id": "t-3"}),
                ],
            ),
            Turn(
                user_message="show me the diff for that",
                expected_tools=[
                    ExpectedTool(name="get_task_diff", args={"task_id": "t-3"}),
                ],
            ),
        ],
        category="multi_step",
        tags=["task", "result", "diff", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Create task then add dependency ---
    TestCase(
        id="multi-create-task-add-dependency",
        description="Create a task, then add a dependency to it",
        turns=[
            Turn(
                user_message="create a task called 'deploy to staging' for project p-1",
                expected_tools=[
                    ExpectedTool(name="create_task", args={"title": "deploy to staging"}),
                ],
            ),
            Turn(
                user_message="make it depend on task t-2",
                expected_tools=[
                    ExpectedTool(name="add_dependency"),
                ],
            ),
        ],
        category="multi_step",
        tags=["task", "dependency", "creation", "context-carry"],
        difficulty=Difficulty.HARD,
        setup_commands=[
            ("create_project", {"name": "Deploy Project", "project_id": "p-1"}),
            (
                "create_task",
                {
                    "project_id": "p-1",
                    "title": "run integration tests",
                    "task_id": "t-2",
                },
            ),
        ],
    ),
    # --- Check status then stop a failing task ---
    TestCase(
        id="multi-status-then-stop",
        description="Check status, see something failing, then stop it",
        turns=[
            Turn(
                user_message="what's the current status?",
                expected_tools=[ExpectedTool(name="get_status")],
            ),
            Turn(
                user_message="stop task t-4, it's stuck",
                expected_tools=[
                    ExpectedTool(name="stop_task", args={"task_id": "t-4"}),
                ],
            ),
        ],
        category="multi_step",
        tags=["status", "stop", "debugging", "context-carry"],
        difficulty=Difficulty.EASY,
    ),
    # --- Create agent then pause it ---
    TestCase(
        id="multi-create-agent-then-pause",
        description="Create an agent and then immediately pause it",
        turns=[
            Turn(
                user_message="create a new agent called builder-1",
                expected_tools=[
                    ExpectedTool(name="create_agent", args={"name": "builder-1"}),
                ],
            ),
            Turn(
                user_message="actually, pause that agent for now",
                expected_tools=[ExpectedTool(name="pause_agent")],
            ),
        ],
        category="multi_step",
        tags=["agent", "create", "pause", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Create hook then fire it ---
    TestCase(
        id="multi-create-hook-then-fire",
        description="Create a hook, then manually fire it",
        turns=[
            Turn(
                user_message="create a hook called 'notify-on-complete' triggered on task_completed",
                expected_tools=[ExpectedTool(name="create_hook")],
            ),
            Turn(
                user_message="fire that hook now",
                expected_tools=[ExpectedTool(name="fire_hook")],
            ),
        ],
        category="multi_step",
        tags=["hook", "create", "fire", "context-carry"],
        difficulty=Difficulty.HARD,
    ),
    # --- View task then reopen with feedback ---
    TestCase(
        id="multi-view-then-reopen",
        description="View a task result, then reopen it with feedback",
        turns=[
            Turn(
                user_message="show me task t-8",
                expected_tools=[
                    ExpectedTool(name="get_task", args={"task_id": "t-8"}),
                ],
            ),
            Turn(
                user_message="reopen it with feedback: needs better error handling",
                expected_tools=[
                    ExpectedTool(
                        name="reopen_with_feedback",
                        args={
                            "task_id": "t-8",
                            "feedback": "needs better error handling",
                        },
                    ),
                ],
            ),
        ],
        category="multi_step",
        tags=["task", "reopen", "feedback", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- List tasks then archive completed ---
    TestCase(
        id="multi-list-then-archive",
        description="List tasks to review, then archive completed ones",
        turns=[
            Turn(
                user_message="show me all tasks for project p-1",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"project_id": "p-1"}),
                ],
            ),
            Turn(
                user_message="archive the completed ones",
                expected_tools=[
                    ExpectedTool(name="archive_tasks", args={"project_id": "p-1"}),
                ],
            ),
        ],
        category="multi_step",
        tags=["task", "list", "archive", "context-carry"],
        difficulty=Difficulty.MEDIUM,
        setup_commands=[
            ("create_project", {"name": "Cleanup Project", "project_id": "p-1"}),
        ],
    ),
    # --- Write note then read it back ---
    TestCase(
        id="multi-write-note-then-read",
        description="Write a note and then read it back",
        turns=[
            Turn(
                user_message="write a note called 'deployment-checklist' with content 'step 1: run tests'",
                expected_tools=[
                    ExpectedTool(name="write_note"),
                ],
            ),
            Turn(
                user_message="read that note back to me",
                expected_tools=[
                    ExpectedTool(name="read_note"),
                ],
            ),
        ],
        category="multi_step",
        tags=["notes", "write", "read", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Check chain health then view dependencies ---
    TestCase(
        id="multi-health-then-dependencies",
        description="Check chain health, then drill into a specific task's dependencies",
        turns=[
            Turn(
                user_message="check the chain health for project p-1",
                expected_tools=[
                    ExpectedTool(name="get_chain_health", args={"project_id": "p-1"}),
                ],
            ),
            Turn(
                user_message="what are the dependencies for task t-10?",
                expected_tools=[
                    ExpectedTool(
                        name="get_task_dependencies",
                        args={"task_id": "t-10"},
                    ),
                ],
            ),
        ],
        category="multi_step",
        tags=["health", "dependencies", "investigation"],
        difficulty=Difficulty.EASY,
    ),
    # --- Three-turn task lifecycle: create, view, stop ---
    TestCase(
        id="multi-task-lifecycle-create-view-stop",
        description="Create a task, view it, then stop it - full lifecycle in three turns",
        turns=[
            Turn(
                user_message="create a task 'benchmark API endpoints' for project p-1",
                expected_tools=[
                    ExpectedTool(name="create_task", args={"title": "benchmark API endpoints"}),
                ],
            ),
            Turn(
                user_message="show me that task",
                expected_tools=[ExpectedTool(name="get_task")],
            ),
            Turn(
                user_message="stop it, the benchmarks aren't ready yet",
                expected_tools=[ExpectedTool(name="stop_task")],
            ),
        ],
        category="multi_step",
        tags=["task", "lifecycle", "create", "view", "stop"],
        difficulty=Difficulty.HARD,
        setup_commands=[
            ("create_project", {"name": "Perf Project", "project_id": "p-1"}),
        ],
    ),
    # --- Git log then diff ---
    TestCase(
        id="multi-git-log-then-diff",
        description="View git log, then view diff for a specific commit",
        turns=[
            Turn(
                user_message="show me the git log for project p-1",
                expected_tools=[
                    ExpectedTool(name="git_log", args={"project_id": "p-1"}),
                ],
            ),
            Turn(
                user_message="show me the diff for that last commit",
                expected_tools=[ExpectedTool(name="git_diff")],
            ),
        ],
        category="multi_step",
        tags=["git", "log", "diff", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Edit project then set active ---
    TestCase(
        id="multi-edit-project-then-set-active",
        description="Edit a project name, then set it as the active project",
        turns=[
            Turn(
                user_message="rename project p-1 to 'Main Service'",
                expected_tools=[
                    ExpectedTool(
                        name="edit_project",
                        args={"project_id": "p-1", "name": "Main Service"},
                    ),
                ],
            ),
            Turn(
                user_message="set it as the active project",
                expected_tools=[
                    ExpectedTool(name="set_active_project", args={"project_id": "p-1"}),
                ],
            ),
        ],
        category="multi_step",
        tags=["project", "edit", "active", "context-carry"],
        difficulty=Difficulty.MEDIUM,
        setup_commands=[
            ("create_project", {"name": "Old Name", "project_id": "p-1"}),
        ],
    ),
    # --- Skip task then restart it ---
    TestCase(
        id="multi-skip-then-restart",
        description="Skip a task, then change mind and restart it",
        turns=[
            Turn(
                user_message="skip task t-6",
                expected_tools=[
                    ExpectedTool(name="skip_task", args={"task_id": "t-6"}),
                ],
            ),
            Turn(
                user_message="actually, restart that task instead",
                expected_tools=[
                    ExpectedTool(name="restart_task"),
                ],
            ),
        ],
        category="multi_step",
        tags=["task", "skip", "restart", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- List workspaces then release one ---
    TestCase(
        id="multi-list-workspaces-then-release",
        description="List workspaces, then release one based on the listing",
        turns=[
            Turn(
                user_message="show me all workspaces",
                expected_tools=[ExpectedTool(name="list_workspaces")],
            ),
            Turn(
                user_message="release the first one",
                expected_tools=[ExpectedTool(name="release_workspace")],
            ),
        ],
        category="multi_step",
        tags=["workspace", "list", "release", "context-carry"],
        difficulty=Difficulty.HARD,
    ),
    # -----------------------------------------------------------------------
    # Supervisor-specific multi-step workflows (post-refactor additions)
    # -----------------------------------------------------------------------
    # --- Create task then approve it ---
    TestCase(
        id="multi-create-task-then-approve",
        description="Create a task requiring approval, then approve it after completion",
        setup_commands=[("create_project", {"name": "ApprovalProj"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="create a task to refactor the auth module, require approval before merge",
                expected_tools=[
                    ExpectedTool(name="create_task", args={"require_approval": True}),
                ],
            ),
            Turn(
                user_message="looks good, approve that task",
                expected_tools=[ExpectedTool(name="approve_task")],
            ),
        ],
        category="multi_step",
        tags=["task", "approval", "supervisor-workflow", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Create task then reopen with feedback ---
    TestCase(
        id="multi-create-task-then-reopen",
        description="Create a task, then send it back for rework with feedback",
        setup_commands=[("create_project", {"name": "ReworkProj"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="create a task to add unit tests for the scheduler",
                expected_tools=[ExpectedTool(name="create_task")],
            ),
            Turn(
                user_message="that task needs more work — also cover edge cases for empty queues",
                expected_tools=[
                    ExpectedTool(name="reopen_with_feedback"),
                ],
            ),
        ],
        category="multi_step",
        tags=["task", "reopen", "feedback", "supervisor-workflow", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Recall memory then create task from findings ---
    TestCase(
        id="multi-memory-search-then-task",
        description="Recall memory for context, then create a task based on findings",
        setup_commands=[("create_project", {"name": "MemProj"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="search memory for recent test failures",
                expected_tools=[ExpectedTool(name="memory_recall")],
            ),
            Turn(
                user_message="ok, create a task to fix those test failures",
                expected_tools=[ExpectedTool(name="create_task")],
            ),
        ],
        category="multi_step",
        tags=["memory", "task", "supervisor-workflow", "context-carry"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Browse tools then load a category ---
    TestCase(
        id="multi-browse-tools-then-load",
        description="Browse available tool categories, then load one",
        turns=[
            Turn(
                user_message="what tool categories are available?",
                expected_tools=[ExpectedTool(name="browse_tools")],
            ),
            Turn(
                user_message="load the git tools",
                expected_tools=[
                    ExpectedTool(name="load_tools", args={"category": "git"}),
                ],
            ),
        ],
        category="multi_step",
        tags=["tools", "browse", "load", "supervisor-workflow", "context-carry"],
        difficulty=Difficulty.EASY,
    ),
]
