"""Test cases for archive operations (supervisor evaluation).

Covers: archive_tasks, archive_task, list_archived, restore_task.

12 test cases: verified against current supervisor-based architecture.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- archive_tasks (bulk) ---
    TestCase(
        id="archive-bulk-completed",
        description="Archive all completed tasks using explicit phrasing",
        turns=[
            Turn(
                user_message="archive all completed tasks",
                expected_tools=[ExpectedTool(name="archive_tasks")],
            ),
        ],
        category="archive",
        tags=["archive", "bulk"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="archive-clean-up-old",
        description="Archive tasks using 'clean up old tasks' phrasing",
        turns=[
            Turn(
                user_message="clean up old tasks",
                expected_tools=[ExpectedTool(name="archive_tasks")],
            ),
        ],
        category="archive",
        tags=["archive", "bulk", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="archive-bulk-for-project",
        description="Archive completed tasks for a specific project",
        turns=[
            Turn(
                user_message="archive all done tasks for project p-1",
                expected_tools=[ExpectedTool(name="archive_tasks", args={"project_id": "p-1"})],
            ),
        ],
        category="archive",
        tags=["archive", "bulk", "project-scoped"],
        difficulty=Difficulty.EASY,
        setup_commands=[("create_project", {"name": "Test Project", "project_id": "p-1"})],
    ),
    TestCase(
        id="archive-sweep-finished",
        description="Archive tasks using 'sweep up finished tasks' phrasing",
        turns=[
            Turn(
                user_message="sweep up all the finished tasks",
                expected_tools=[ExpectedTool(name="archive_tasks")],
            ),
        ],
        category="archive",
        tags=["archive", "bulk", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- archive_task (single) ---
    TestCase(
        id="archive-single-task",
        description="Archive a single specific task by ID",
        turns=[
            Turn(
                user_message="archive task t-1",
                expected_tools=[ExpectedTool(name="archive_task", args={"task_id": "t-1"})],
            ),
        ],
        category="archive",
        tags=["archive", "single"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="archive-single-task-verbose",
        description="Archive a single task with verbose phrasing",
        turns=[
            Turn(
                user_message="please move task t-5 to the archive",
                expected_tools=[ExpectedTool(name="archive_task", args={"task_id": "t-5"})],
            ),
        ],
        category="archive",
        tags=["archive", "single", "indirect"],
        difficulty=Difficulty.EASY,
    ),
    # --- list_archived ---
    TestCase(
        id="archive-list-archived",
        description="List archived tasks using explicit phrasing",
        turns=[
            Turn(
                user_message="show archived tasks",
                expected_tools=[ExpectedTool(name="list_archived")],
            ),
        ],
        category="archive",
        tags=["archive", "list"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="archive-list-archived-alt",
        description="List archived tasks using 'what have we archived' phrasing",
        turns=[
            Turn(
                user_message="what tasks have been archived?",
                expected_tools=[ExpectedTool(name="list_archived")],
            ),
        ],
        category="archive",
        tags=["archive", "list", "indirect"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="archive-list-for-project",
        description="List archived tasks for a specific project",
        turns=[
            Turn(
                user_message="show me archived tasks for project p-2",
                expected_tools=[
                    ExpectedTool(name="list_archived", args={"project_id": "p-2"}),
                ],
            ),
        ],
        category="archive",
        tags=["archive", "list", "project-scoped"],
        difficulty=Difficulty.EASY,
    ),
    # --- restore_task ---
    TestCase(
        id="archive-restore-task",
        description="Restore a task from the archive by ID",
        turns=[
            Turn(
                user_message="restore task t-1 from archive",
                expected_tools=[ExpectedTool(name="restore_task", args={"task_id": "t-1"})],
            ),
        ],
        category="archive",
        tags=["archive", "restore"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="archive-restore-bring-back",
        description="Restore a task using 'bring back' phrasing",
        turns=[
            Turn(
                user_message="bring back task t-3",
                expected_tools=[ExpectedTool(name="restore_task", args={"task_id": "t-3"})],
            ),
        ],
        category="archive",
        tags=["archive", "restore", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="archive-restore-unarchive",
        description="Restore a task using 'unarchive' phrasing",
        turns=[
            Turn(
                user_message="unarchive task t-7",
                expected_tools=[ExpectedTool(name="restore_task", args={"task_id": "t-7"})],
            ),
        ],
        category="archive",
        tags=["archive", "restore", "indirect"],
        difficulty=Difficulty.EASY,
    ),
]
