"""Test cases where behavior depends on the active_project context (supervisor evaluation).

These verify that when an active project is set, the Supervisor correctly passes the
project_id to tools that need it. Also tests behavior when no project is set,
and context switching between projects across turns.

12 test cases: verified against current supervisor-based architecture.
Active project context is a core Supervisor concept set via set_active_project.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- With active project set (case-level) ---
    TestCase(
        id="ctx-create-task-with-active",
        description="Create task inherits active project p-1 without explicit mention",
        turns=[
            Turn(
                user_message="create a task to fix the auth module",
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={"project_id": "p-1"},
                    ),
                ],
                not_expected_tools=["create_project"],
            ),
        ],
        category="context_dependent",
        tags=["task", "creation", "active-project"],
        difficulty=Difficulty.MEDIUM,
        active_project="p-1",
        setup_commands=[
            ("create_project", {"name": "Auth Service", "project_id": "p-1"}),
        ],
    ),
    TestCase(
        id="ctx-list-notes-with-active",
        description="List notes scoped to active project p-1",
        turns=[
            Turn(
                user_message="list notes",
                expected_tools=[
                    ExpectedTool(name="list_notes", args={"project_id": "p-1"}),
                ],
            ),
        ],
        category="context_dependent",
        tags=["notes", "list", "active-project"],
        difficulty=Difficulty.MEDIUM,
        active_project="p-1",
        setup_commands=[
            ("create_project", {"name": "Notes Project", "project_id": "p-1"}),
        ],
    ),
    TestCase(
        id="ctx-chain-health-with-active",
        description="Chain health check uses active project p-1",
        turns=[
            Turn(
                user_message="check chain health",
                expected_tools=[
                    ExpectedTool(name="get_chain_health", args={"project_id": "p-1"}),
                ],
            ),
        ],
        category="context_dependent",
        tags=["health", "active-project"],
        difficulty=Difficulty.MEDIUM,
        active_project="p-1",
        setup_commands=[
            ("create_project", {"name": "Health Project", "project_id": "p-1"}),
        ],
    ),
    TestCase(
        id="ctx-list-tasks-with-active",
        description="List tasks scoped to active project",
        turns=[
            Turn(
                user_message="show me the tasks",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"project_id": "p-1"}),
                ],
            ),
        ],
        category="context_dependent",
        tags=["task", "list", "active-project"],
        difficulty=Difficulty.EASY,
        active_project="p-1",
        setup_commands=[
            ("create_project", {"name": "Task Project", "project_id": "p-1"}),
        ],
    ),
    TestCase(
        id="ctx-git-status-with-active",
        description="Git status uses active project for repo context",
        turns=[
            Turn(
                user_message="what's the git status?",
                expected_tools=[
                    ExpectedTool(name="get_git_status", args={"project_id": "p-1"}),
                ],
            ),
        ],
        category="context_dependent",
        tags=["git", "status", "active-project"],
        difficulty=Difficulty.EASY,
        active_project="p-1",
        setup_commands=[
            ("create_project", {"name": "Git Project", "project_id": "p-1"}),
        ],
    ),
    # --- Without active project ---
    TestCase(
        id="ctx-list-notes-no-active",
        description="List notes with no active project - LLM should ask or call without project_id",
        turns=[
            Turn(
                user_message="list notes",
                expected_tools=[ExpectedTool(name="list_notes")],
                not_expected_tools=["create_project", "delete_note"],
            ),
        ],
        category="context_dependent",
        tags=["notes", "list", "no-active-project"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="ctx-create-task-no-active",
        description="Create task with no active project - needs to specify or ask",
        turns=[
            Turn(
                user_message="create a task to write unit tests",
                expected_tools=[ExpectedTool(name="create_task")],
                not_expected_tools=["create_project"],
            ),
        ],
        category="context_dependent",
        tags=["task", "creation", "no-active-project"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Context switching between projects (turn-level override) ---
    TestCase(
        id="ctx-switch-project-between-turns",
        description="Active project changes between turns - tools should reflect the switch",
        turns=[
            Turn(
                user_message="list tasks",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"project_id": "p-1"}),
                ],
                active_project="p-1",
            ),
            Turn(
                user_message="now list tasks for this project",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"project_id": "p-2"}),
                ],
                active_project="p-2",
            ),
        ],
        category="context_dependent",
        tags=["task", "list", "context-switch"],
        difficulty=Difficulty.HARD,
        setup_commands=[
            ("create_project", {"name": "Frontend", "project_id": "p-1"}),
            ("create_project", {"name": "Backend", "project_id": "p-2"}),
        ],
    ),
    TestCase(
        id="ctx-switch-notes-between-turns",
        description="Write note in one project context, read in another",
        turns=[
            Turn(
                user_message="write a note called 'api-design' with content 'REST endpoints'",
                expected_tools=[
                    ExpectedTool(name="write_note"),
                ],
                active_project="p-1",
            ),
            Turn(
                user_message="list notes",
                expected_tools=[
                    ExpectedTool(name="list_notes", args={"project_id": "p-2"}),
                ],
                active_project="p-2",
            ),
        ],
        category="context_dependent",
        tags=["notes", "write", "list", "context-switch"],
        difficulty=Difficulty.HARD,
        setup_commands=[
            ("create_project", {"name": "API Service", "project_id": "p-1"}),
            ("create_project", {"name": "Web App", "project_id": "p-2"}),
        ],
    ),
    # --- Explicit project overrides active project ---
    TestCase(
        id="ctx-explicit-overrides-active",
        description="User explicitly names a project that differs from active - explicit wins",
        turns=[
            Turn(
                user_message="list tasks for project p-2",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"project_id": "p-2"}),
                ],
                not_expected_tools=["create_task"],
            ),
        ],
        category="context_dependent",
        tags=["task", "list", "explicit-override", "active-project"],
        difficulty=Difficulty.MEDIUM,
        active_project="p-1",
        setup_commands=[
            ("create_project", {"name": "Active Project", "project_id": "p-1"}),
            ("create_project", {"name": "Other Project", "project_id": "p-2"}),
        ],
    ),
    TestCase(
        id="ctx-archive-with-active",
        description="Archive tasks uses active project context",
        turns=[
            Turn(
                user_message="archive completed tasks",
                expected_tools=[
                    ExpectedTool(name="archive_tasks"),
                ],
            ),
        ],
        category="context_dependent",
        tags=["archive", "active-project"],
        difficulty=Difficulty.MEDIUM,
        active_project="p-1",
        setup_commands=[
            ("create_project", {"name": "Archive Context", "project_id": "p-1"}),
        ],
    ),
    TestCase(
        id="ctx-write-note-with-active",
        description="Write a note inherits active project context",
        turns=[
            Turn(
                user_message="write a note called 'todo' with content 'fix the login bug'",
                expected_tools=[
                    ExpectedTool(name="write_note"),
                ],
            ),
        ],
        category="context_dependent",
        tags=["notes", "write", "active-project"],
        difficulty=Difficulty.MEDIUM,
        active_project="p-1",
        setup_commands=[
            ("create_project", {"name": "Notes Context", "project_id": "p-1"}),
        ],
    ),
]
