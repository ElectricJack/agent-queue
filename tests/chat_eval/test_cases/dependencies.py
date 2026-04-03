"""Test cases for task dependency management tools (supervisor evaluation).

Covers: get_task_dependencies, add_dependency, remove_dependency, get_chain_health.

13 test cases: verified against current supervisor-based architecture.
Dependencies control task execution order in the queue.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # -----------------------------------------------------------------------
    # get_task_dependencies — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="dep-get-direct",
        description="Get dependencies for a task by ID",
        category="dependencies",
        difficulty=Difficulty.TRIVIAL,
        tags=["get_task_dependencies", "read"],
        setup_commands=[
            ("create_project", {"name": "DepProj"}),
            ("create_task", {"project_id": "p-1", "title": "Task A", "description": "A"}),
        ],
        turns=[
            Turn(
                user_message="show deps for t-1",
                expected_tools=[
                    ExpectedTool(
                        name="get_task_dependencies",
                        args={"task_id": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-get-whats-blocking",
        description="Ask what is blocking a task",
        category="dependencies",
        difficulty=Difficulty.EASY,
        tags=["get_task_dependencies", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "BlockProj"}),
            ("create_task", {"project_id": "p-1", "title": "Stuck Task", "description": "Stuck"}),
        ],
        turns=[
            Turn(
                user_message="what's blocking t-1?",
                expected_tools=[
                    ExpectedTool(
                        name="get_task_dependencies",
                        args={"task_id": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-get-depends-on",
        description="Ask what a task depends on",
        category="dependencies",
        difficulty=Difficulty.EASY,
        tags=["get_task_dependencies", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "DepsOnProj"}),
            ("create_task", {"project_id": "p-1", "title": "Dependent", "description": "Has deps"}),
        ],
        turns=[
            Turn(
                user_message="what does t-1 depend on?",
                expected_tools=[
                    ExpectedTool(
                        name="get_task_dependencies",
                        args={"task_id": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-get-downstream",
        description="Ask what tasks depend on a given task",
        category="dependencies",
        difficulty=Difficulty.MEDIUM,
        tags=["get_task_dependencies", "read", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "DownstreamProj"}),
            ("create_task", {"project_id": "p-1", "title": "Core Task", "description": "Core"}),
        ],
        turns=[
            Turn(
                user_message="what tasks are waiting on t-1 to finish?",
                expected_tools=[
                    ExpectedTool(
                        name="get_task_dependencies",
                        args={"task_id": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # add_dependency — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="dep-add-direct",
        description="Add a dependency between two tasks",
        category="dependencies",
        difficulty=Difficulty.EASY,
        tags=["add_dependency", "write"],
        setup_commands=[
            ("create_project", {"name": "AddDepProj"}),
            ("create_task", {"project_id": "p-1", "title": "Task A", "description": "First"}),
            ("create_task", {"project_id": "p-1", "title": "Task B", "description": "Second"}),
        ],
        turns=[
            Turn(
                user_message="make t-2 depend on t-1",
                expected_tools=[
                    ExpectedTool(
                        name="add_dependency",
                        args={"task_id": "t-2", "depends_on": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-add-natural-before",
        description="Add dependency using 'before' phrasing",
        category="dependencies",
        difficulty=Difficulty.MEDIUM,
        tags=["add_dependency", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "BeforeProj"}),
            ("create_task", {"project_id": "p-1", "title": "Setup", "description": "Setup first"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Deploy", "description": "Deploy after"},
            ),
        ],
        turns=[
            Turn(
                user_message="t-1 needs to finish before t-2 can start",
                expected_tools=[
                    ExpectedTool(
                        name="add_dependency",
                        args={"task_id": "t-2", "depends_on": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-add-natural-cant-start",
        description="Add dependency using 'cannot start until' phrasing",
        category="dependencies",
        difficulty=Difficulty.MEDIUM,
        tags=["add_dependency", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "CantStartProj"}),
            ("create_task", {"project_id": "p-1", "title": "Build", "description": "Build first"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Test", "description": "Test after build"},
            ),
        ],
        turns=[
            Turn(
                user_message="t-2 can't start until t-1 is done",
                expected_tools=[
                    ExpectedTool(
                        name="add_dependency",
                        args={"task_id": "t-2", "depends_on": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # remove_dependency — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="dep-remove-direct",
        description="Remove a dependency between two tasks",
        category="dependencies",
        difficulty=Difficulty.EASY,
        tags=["remove_dependency", "write"],
        setup_commands=[
            ("create_project", {"name": "RemDepProj"}),
            ("create_task", {"project_id": "p-1", "title": "Task A", "description": "A"}),
            ("create_task", {"project_id": "p-1", "title": "Task B", "description": "B"}),
            ("add_dependency", {"task_id": "t-2", "depends_on": "t-1"}),
        ],
        turns=[
            Turn(
                user_message="remove the dependency of t-2 on t-1",
                expected_tools=[
                    ExpectedTool(
                        name="remove_dependency",
                        args={"task_id": "t-2", "depends_on": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-remove-unlink",
        description="Unlink tasks using natural language",
        category="dependencies",
        difficulty=Difficulty.MEDIUM,
        tags=["remove_dependency", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "UnlinkProj"}),
            ("create_task", {"project_id": "p-1", "title": "Independent A", "description": "A"}),
            ("create_task", {"project_id": "p-1", "title": "Independent B", "description": "B"}),
            ("add_dependency", {"task_id": "t-2", "depends_on": "t-1"}),
        ],
        turns=[
            Turn(
                user_message="t-2 no longer needs to wait for t-1, remove the link",
                expected_tools=[
                    ExpectedTool(
                        name="remove_dependency",
                        args={"task_id": "t-2", "depends_on": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # get_chain_health — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="dep-health-direct",
        description="Check chain health with no filters",
        category="dependencies",
        difficulty=Difficulty.TRIVIAL,
        tags=["get_chain_health", "read"],
        turns=[
            Turn(
                user_message="check chain health",
                expected_tools=[
                    ExpectedTool(name="get_chain_health"),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-health-for-project",
        description="Check chain health for a specific project",
        category="dependencies",
        difficulty=Difficulty.EASY,
        tags=["get_chain_health", "read"],
        setup_commands=[("create_project", {"name": "HealthProj"})],
        turns=[
            Turn(
                user_message="check dependency chain health for project p-1",
                expected_tools=[
                    ExpectedTool(
                        name="get_chain_health",
                        args={"project_id": "p-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-health-for-task",
        description="Check chain health for a specific task",
        category="dependencies",
        difficulty=Difficulty.EASY,
        tags=["get_chain_health", "read"],
        setup_commands=[
            ("create_project", {"name": "TaskHealth"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Blocked", "description": "Blocked task"},
            ),
        ],
        turns=[
            Turn(
                user_message="check the health of the dependency chain around t-1",
                expected_tools=[
                    ExpectedTool(
                        name="get_chain_health",
                        args={"task_id": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="dep-health-natural",
        description="Ask about stuck dependency chains",
        category="dependencies",
        difficulty=Difficulty.MEDIUM,
        tags=["get_chain_health", "read", "natural-language"],
        turns=[
            Turn(
                user_message="are there any stuck dependency chains?",
                expected_tools=[
                    ExpectedTool(name="get_chain_health"),
                ],
            ),
        ],
    ),
]
