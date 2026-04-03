"""Test cases for task CRUD and inspection tools (supervisor evaluation).

Covers: list_tasks, list_active_tasks_all_projects, get_task_tree, create_task,
        get_task, edit_task, delete_task, get_task_result, get_task_diff, get_agent_error

49 test cases: all verified against current supervisor-based architecture.
Tasks are the core unit of work — users ask the Supervisor to create, inspect,
and manage tasks which are then assigned to agents via the task queue.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # -----------------------------------------------------------------------
    # list_tasks — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="task-list-trivial",
        description="Direct 'list tasks' command",
        category="tasks",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_tasks", "read"],
        setup_commands=[("create_project", {"name": "TaskProj"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="list tasks",
                expected_tools=[ExpectedTool(name="list_tasks")],
            ),
        ],
    ),
    TestCase(
        id="task-list-for-project",
        description="List tasks for a specific project",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["list_tasks", "read"],
        setup_commands=[("create_project", {"name": "ProjectA"})],
        turns=[
            Turn(
                user_message="show tasks for project p-1",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-whats-running",
        description="Ask what tasks are currently running",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["list_tasks", "list_active_tasks_all_projects", "read", "natural-language"],
        setup_commands=[("create_project", {"name": "Active"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what tasks are currently running?",
                expected_tools=[
                    # Either list_tasks with status filter or list_active_tasks_all_projects
                    ExpectedTool(name="list_tasks"),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-show-all",
        description="Show all tasks including completed",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["list_tasks", "read"],
        setup_commands=[("create_project", {"name": "AllTasks"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show all tasks including completed",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"include_completed": True}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-completed-only",
        description="Show only completed tasks",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_tasks", "read"],
        setup_commands=[("create_project", {"name": "Done"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show me only the completed tasks",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"completed_only": True}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-by-status-ready",
        description="Filter tasks by READY status",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["list_tasks", "read"],
        setup_commands=[("create_project", {"name": "StatusFilter"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show tasks that are ready to go",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"status": "READY"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-tree-mode",
        description="List tasks in tree view",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_tasks", "read", "display-mode"],
        setup_commands=[("create_project", {"name": "TreeProj"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show me a tree view of all tasks for p-1",
                expected_tools=[
                    ExpectedTool(
                        name="list_tasks",
                        args={"project_id": "p-1", "display_mode": "tree"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-compact-mode",
        description="List tasks in compact mode with progress bars",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_tasks", "read", "display-mode"],
        setup_commands=[("create_project", {"name": "CompactProj"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show a compact overview of tasks for p-1 with progress bars",
                expected_tools=[
                    ExpectedTool(
                        name="list_tasks",
                        args={"project_id": "p-1", "display_mode": "compact"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-with-deps",
        description="List tasks with dependency annotations",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_tasks", "read", "dependencies"],
        setup_commands=[("create_project", {"name": "DepsView"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show tasks with their dependencies for p-1",
                expected_tools=[
                    ExpectedTool(
                        name="list_tasks",
                        args={"project_id": "p-1", "show_dependencies": True},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-failed",
        description="Ask about failed tasks",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["list_tasks", "read", "natural-language"],
        setup_commands=[("create_project", {"name": "FailCheck"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="are there any failed tasks?",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"status": "FAILED"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-blocked",
        description="Ask about blocked tasks",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["list_tasks", "read", "natural-language"],
        setup_commands=[("create_project", {"name": "BlockCheck"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what tasks are blocked?",
                expected_tools=[
                    ExpectedTool(name="list_tasks", args={"status": "BLOCKED"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-awaiting-approval",
        description="Check for tasks awaiting approval",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_tasks", "read", "natural-language"],
        setup_commands=[("create_project", {"name": "ApprovalCheck"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message="any tasks waiting for my approval?",
                expected_tools=[
                    ExpectedTool(name="list_tasks"),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # list_active_tasks_all_projects — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="task-list-all-projects-direct",
        description="List active tasks across all projects",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["list_active_tasks_all_projects", "read"],
        turns=[
            Turn(
                user_message="list tasks across all projects",
                expected_tools=[
                    ExpectedTool(name="list_active_tasks_all_projects"),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-all-projects-natural",
        description="Ask for a cross-project task overview",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_active_tasks_all_projects", "read", "natural-language"],
        turns=[
            Turn(
                user_message="give me an overview of everything that's in progress across all projects",
                expected_tools=[
                    ExpectedTool(name="list_active_tasks_all_projects"),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-list-all-projects-include-completed",
        description="List all tasks across projects including completed",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_active_tasks_all_projects", "read"],
        turns=[
            Turn(
                user_message="show all tasks across every project, including completed ones",
                expected_tools=[
                    ExpectedTool(
                        name="list_active_tasks_all_projects",
                        args={"include_completed": True},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # get_task_tree — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="task-tree-direct",
        description="Get task tree for a specific task",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["get_task_tree", "read"],
        setup_commands=[
            ("create_project", {"name": "TreeTest"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Root Task", "description": "A root task"},
            ),
        ],
        turns=[
            Turn(
                user_message="show the full task tree for t-1",
                expected_tools=[
                    ExpectedTool(name="get_task_tree", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-tree-subtasks",
        description="Ask about subtasks of a specific task",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["get_task_tree", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "SubTree"}),
            ("create_task", {"project_id": "p-1", "title": "Parent", "description": "Parent task"}),
        ],
        turns=[
            Turn(
                user_message="what are the subtasks of t-1?",
                expected_tools=[
                    ExpectedTool(name="get_task_tree", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-tree-compact",
        description="Get a compact tree view of a task",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["get_task_tree", "read"],
        setup_commands=[
            ("create_project", {"name": "CompactTree"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Big Plan", "description": "Task with subtasks"},
            ),
        ],
        turns=[
            Turn(
                user_message="show a compact summary of t-1's subtasks",
                expected_tools=[
                    ExpectedTool(
                        name="get_task_tree",
                        args={"task_id": "t-1", "compact": True},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # create_task — TRIVIAL / EASY / MEDIUM / HARD
    # -----------------------------------------------------------------------
    TestCase(
        id="task-create-simple",
        description="Create a task with title and description",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["create_task", "write"],
        turns=[
            Turn(
                user_message="create a task to fix the login bug",
                expected_tools=[
                    ExpectedTool(name="create_task"),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-detailed",
        description="Create a task with explicit title and description",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["create_task", "write", "multi-arg"],
        setup_commands=[("create_project", {"name": "BugProject"})],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "create a task titled 'Fix password reset' with description "
                    "'The password reset email link expires too quickly, increase "
                    "timeout to 24 hours'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={
                            "title": "Fix password reset",
                            "description": "The password reset email link expires too quickly, increase timeout to 24 hours",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-with-project",
        description="Create a task assigned to a specific project",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["create_task", "write"],
        setup_commands=[("create_project", {"name": "Targeted"})],
        turns=[
            Turn(
                user_message="create a task in project p-1 to add unit tests for the auth module",
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={"project_id": "p-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-with-priority",
        description="Create a high-priority task",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["create_task", "write", "multi-arg"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="create a high priority task to fix the production crash, priority 10",
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={"priority": 10},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-with-type",
        description="Create a task with an explicit task type",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["create_task", "write", "multi-arg"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="create a bugfix task to fix the null pointer in UserService",
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={"task_type": "bugfix"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-with-approval",
        description="Create a task requiring approval before merge",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["create_task", "write", "multi-arg"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "create a task to refactor the database layer, "
                    "it needs my approval before merging"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={"requires_approval": True},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-full",
        description="Create a task with many parameters",
        category="tasks",
        difficulty=Difficulty.HARD,
        tags=["create_task", "write", "multi-arg"],
        setup_commands=[("create_project", {"name": "FullTask"})],
        turns=[
            Turn(
                user_message=(
                    "in project p-1, create a bugfix task titled 'Fix memory leak' "
                    "with description 'The worker process leaks ~50MB/hour', "
                    "priority 5, and it needs approval"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={
                            "project_id": "p-1",
                            "title": "Fix memory leak",
                            "priority": 5,
                            "task_type": "bugfix",
                            "requires_approval": True,
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-quick",
        description="Create a quick standalone task without specifying project",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["create_task", "write", "natural-language"],
        turns=[
            Turn(
                user_message="add a quick task: update the README with new setup instructions",
                expected_tools=[
                    ExpectedTool(name="create_task"),
                ],
                not_expected_tools=["list_tasks"],
            ),
        ],
    ),
    TestCase(
        id="task-create-natural-imperative",
        description="Natural imperative: 'add a task to...'",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["create_task", "write", "natural-language"],
        turns=[
            Turn(
                user_message="add a task to migrate the database from PostgreSQL to MySQL",
                expected_tools=[
                    ExpectedTool(name="create_task"),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-natural-question",
        description="Natural question form: 'can you create a task...'",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["create_task", "write", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="can you create a task to implement the search feature?",
                expected_tools=[
                    ExpectedTool(name="create_task"),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-research",
        description="Create a research task",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["create_task", "write"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "create a research task to investigate which caching "
                    "strategy is best for our API"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={"task_type": "research"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-create-plan",
        description="Create a plan task",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["create_task", "write"],
        turns=[
            Turn(
                user_message="create a plan task to design the new microservices architecture",
                expected_tools=[
                    ExpectedTool(
                        name="create_task",
                        args={"task_type": "plan"},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # get_task — TRIVIAL / EASY
    # -----------------------------------------------------------------------
    TestCase(
        id="task-get-by-id",
        description="Get task details by ID",
        category="tasks",
        difficulty=Difficulty.TRIVIAL,
        tags=["get_task", "read"],
        setup_commands=[
            ("create_project", {"name": "Inspect"}),
            ("create_task", {"project_id": "p-1", "title": "Test Task", "description": "A test"}),
        ],
        turns=[
            Turn(
                user_message="show me task t-1",
                expected_tools=[
                    ExpectedTool(name="get_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-get-details",
        description="Ask for details of a task",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["get_task", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "Details"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Important Task", "description": "Details here"},
            ),
        ],
        turns=[
            Turn(
                user_message="what's the status of t-1?",
                expected_tools=[
                    ExpectedTool(name="get_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-get-tell-me",
        description="'tell me about' form",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["get_task", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "TellMe"}),
            ("create_task", {"project_id": "p-1", "title": "Some Task", "description": "Desc"}),
        ],
        turns=[
            Turn(
                user_message="tell me about task t-1",
                expected_tools=[
                    ExpectedTool(name="get_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # edit_task — EASY / MEDIUM / HARD
    # -----------------------------------------------------------------------
    TestCase(
        id="task-edit-rename",
        description="Rename a task",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["edit_task", "write"],
        setup_commands=[
            ("create_project", {"name": "EditProj"}),
            ("create_task", {"project_id": "p-1", "title": "Old Title", "description": "Desc"}),
        ],
        turns=[
            Turn(
                user_message="rename task t-1 to 'New Title'",
                expected_tools=[
                    ExpectedTool(
                        name="edit_task",
                        args={"task_id": "t-1", "title": "New Title"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-edit-description",
        description="Update task description",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_task", "write"],
        setup_commands=[
            ("create_project", {"name": "DescEdit"}),
            ("create_task", {"project_id": "p-1", "title": "Task", "description": "Old desc"}),
        ],
        turns=[
            Turn(
                user_message=(
                    "update the description of t-1 to 'Fix the race condition "
                    "in the connection pool by adding a mutex'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="edit_task",
                        args={
                            "task_id": "t-1",
                            "description": "Fix the race condition in the connection pool by adding a mutex",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-edit-priority",
        description="Change task priority",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["edit_task", "write"],
        setup_commands=[
            ("create_project", {"name": "PrioEdit"}),
            ("create_task", {"project_id": "p-1", "title": "Task", "description": "Desc"}),
        ],
        turns=[
            Turn(
                user_message="set task t-1 priority to 1",
                expected_tools=[
                    ExpectedTool(
                        name="edit_task",
                        args={"task_id": "t-1", "priority": 1},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-edit-type",
        description="Change task type",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_task", "write"],
        setup_commands=[
            ("create_project", {"name": "TypeEdit"}),
            ("create_task", {"project_id": "p-1", "title": "Task", "description": "Desc"}),
        ],
        turns=[
            Turn(
                user_message="mark task t-1 as a bugfix",
                expected_tools=[
                    ExpectedTool(
                        name="edit_task",
                        args={"task_id": "t-1", "task_type": "bugfix"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-edit-multi",
        description="Edit multiple task properties at once",
        category="tasks",
        difficulty=Difficulty.HARD,
        tags=["edit_task", "write", "multi-arg"],
        setup_commands=[
            ("create_project", {"name": "MultiEdit"}),
            ("create_task", {"project_id": "p-1", "title": "Task", "description": "Desc"}),
        ],
        turns=[
            Turn(
                user_message=(
                    "update task t-1: rename to 'Critical Bug', set priority "
                    "to 1, and change type to bugfix"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="edit_task",
                        args={
                            "task_id": "t-1",
                            "title": "Critical Bug",
                            "priority": 1,
                            "task_type": "bugfix",
                        },
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # delete_task — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="task-delete-by-id",
        description="Delete a task by ID",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["delete_task", "write", "destructive"],
        setup_commands=[
            ("create_project", {"name": "DelProj"}),
            ("create_task", {"project_id": "p-1", "title": "Unwanted", "description": "Delete me"}),
        ],
        turns=[
            Turn(
                user_message="delete task t-1",
                expected_tools=[
                    ExpectedTool(name="delete_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-delete-natural",
        description="Delete a task with natural language",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["delete_task", "write", "destructive", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "CleanProj"}),
            ("create_task", {"project_id": "p-1", "title": "Duplicate", "description": "Dupe"}),
        ],
        turns=[
            Turn(
                user_message="remove task t-1, it's a duplicate",
                expected_tools=[
                    ExpectedTool(name="delete_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # get_task_result — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="task-result-direct",
        description="Get task result by ID",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["get_task_result", "read"],
        setup_commands=[
            ("create_project", {"name": "ResultProj"}),
            ("create_task", {"project_id": "p-1", "title": "Done Task", "description": "Finished"}),
        ],
        turns=[
            Turn(
                user_message="show results for task t-1",
                expected_tools=[
                    ExpectedTool(name="get_task_result", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-result-what-did",
        description="Ask what a task did",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["get_task_result", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "OutputProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Mystery Task", "description": "What happened"},
            ),
        ],
        turns=[
            Turn(
                user_message="what did task t-1 produce?",
                expected_tools=[
                    ExpectedTool(name="get_task_result", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-result-summary",
        description="Ask for a task's output summary",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["get_task_result", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "SumProj"}),
            ("create_task", {"project_id": "p-1", "title": "Completed", "description": "Done"}),
        ],
        turns=[
            Turn(
                user_message="give me the summary of t-1's output",
                expected_tools=[
                    ExpectedTool(name="get_task_result", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # get_task_diff — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="task-diff-direct",
        description="Get diff for a task",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["get_task_diff", "read"],
        setup_commands=[
            ("create_project", {"name": "DiffProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Code Change", "description": "Changed code"},
            ),
        ],
        turns=[
            Turn(
                user_message="show the diff for task t-1",
                expected_tools=[
                    ExpectedTool(name="get_task_diff", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-diff-what-changed",
        description="Ask what code changed for a task",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["get_task_diff", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "CodeReview"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Refactor", "description": "Refactored"},
            ),
        ],
        turns=[
            Turn(
                user_message="what did task t-1 change in the code?",
                expected_tools=[
                    ExpectedTool(name="get_task_diff", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-diff-code-review",
        description="Ask to see the code changes for review",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["get_task_diff", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "Review"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Feature", "description": "New feature"},
            ),
        ],
        turns=[
            Turn(
                user_message="let me see the code changes from t-1 so I can review them",
                expected_tools=[
                    ExpectedTool(name="get_task_diff", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # get_agent_error — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="task-error-direct",
        description="Get the error for a task",
        category="tasks",
        difficulty=Difficulty.EASY,
        tags=["get_agent_error", "read"],
        setup_commands=[
            ("create_project", {"name": "ErrProj"}),
            ("create_task", {"project_id": "p-1", "title": "Broken", "description": "Fails"}),
        ],
        turns=[
            Turn(
                user_message="show me the error for task t-1",
                expected_tools=[
                    ExpectedTool(name="get_agent_error", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-error-why-fail",
        description="Ask why a task failed",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["get_agent_error", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "FailProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Crashing", "description": "Keeps crashing"},
            ),
        ],
        turns=[
            Turn(
                user_message="why did task t-1 fail?",
                expected_tools=[
                    ExpectedTool(name="get_agent_error", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="task-error-whats-wrong",
        description="Ask what went wrong with a task",
        category="tasks",
        difficulty=Difficulty.MEDIUM,
        tags=["get_agent_error", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "DiagProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Errored", "description": "Errored out"},
            ),
        ],
        turns=[
            Turn(
                user_message="what went wrong with t-1? show me the error details",
                expected_tools=[
                    ExpectedTool(name="get_agent_error", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
]
