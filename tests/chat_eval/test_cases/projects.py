"""Test cases for project management tools (supervisor evaluation).

Covers: list_projects, create_project, pause_project, resume_project,
        edit_project, get_project_channels, get_project_for_channel, delete_project

26 test cases: verified against current supervisor-based architecture.
Projects are the top-level organizational unit. The Supervisor manages them
and uses the active_project context for scoped operations.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # -----------------------------------------------------------------------
    # list_projects — TRIVIAL / EASY
    # -----------------------------------------------------------------------
    TestCase(
        id="proj-list-trivial",
        description="Direct 'list projects' command",
        category="projects",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_projects", "read"],
        turns=[
            Turn(
                user_message="list all projects",
                expected_tools=[ExpectedTool(name="list_projects")],
            ),
        ],
    ),
    TestCase(
        id="proj-list-question",
        description="Natural question asking about projects",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["list_projects", "read", "natural-language"],
        turns=[
            Turn(
                user_message="what projects do we have?",
                expected_tools=[ExpectedTool(name="list_projects")],
            ),
        ],
    ),
    TestCase(
        id="proj-list-shorthand",
        description="Short 'projects' command",
        category="projects",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_projects", "read"],
        turns=[
            Turn(
                user_message="projects",
                expected_tools=[ExpectedTool(name="list_projects")],
            ),
        ],
    ),
    TestCase(
        id="proj-list-show-me",
        description="Casual 'show me' phrasing",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["list_projects", "read", "natural-language"],
        turns=[
            Turn(
                user_message="show me all the projects we're working on",
                expected_tools=[ExpectedTool(name="list_projects")],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # create_project — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="proj-create-simple",
        description="Create a project with just a name",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["create_project", "write"],
        turns=[
            Turn(
                user_message="create a project called My Web App",
                expected_tools=[
                    ExpectedTool(name="create_project", args={"name": "My Web App"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-create-quoted-name",
        description="Create a project with a quoted name",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["create_project", "write"],
        turns=[
            Turn(
                user_message='create project "Backend API v2"',
                expected_tools=[
                    ExpectedTool(name="create_project", args={"name": "Backend API v2"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-create-with-repo",
        description="Create a project with name and repo URL",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["create_project", "write", "multi-arg"],
        turns=[
            Turn(
                user_message=(
                    "set up a new project for the frontend rewrite at github.com/org/frontend"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_project",
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-create-with-weight",
        description="Create a project with a custom credit weight",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["create_project", "write", "multi-arg"],
        turns=[
            Turn(
                user_message="create project Data Pipeline with credit weight 2.5",
                expected_tools=[
                    ExpectedTool(
                        name="create_project",
                        args={"name": "Data Pipeline", "credit_weight": 2.5},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-create-full",
        description="Create a project with name, repo URL, and max agents",
        category="projects",
        difficulty=Difficulty.HARD,
        tags=["create_project", "write", "multi-arg"],
        turns=[
            Turn(
                user_message=(
                    "create a project called Mobile App with repo "
                    "https://github.com/acme/mobile-app and max 3 concurrent agents"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_project",
                        args={
                            "name": "Mobile App",
                            "repo_url": "https://github.com/acme/mobile-app",
                            "max_concurrent_agents": 3,
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-create-casual",
        description="Casual request to set up a new project",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["create_project", "write", "natural-language"],
        turns=[
            Turn(
                user_message="I need a new project for the auth service",
                expected_tools=[
                    ExpectedTool(name="create_project"),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # pause_project — TRIVIAL / EASY
    # -----------------------------------------------------------------------
    TestCase(
        id="proj-pause-direct",
        description="Pause a project by ID",
        category="projects",
        difficulty=Difficulty.TRIVIAL,
        tags=["pause_project", "write"],
        setup_commands=[("create_project", {"name": "Pausable"})],
        turns=[
            Turn(
                user_message="pause project p-1",
                expected_tools=[
                    ExpectedTool(name="pause_project", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-pause-natural",
        description="Pause a project with natural language",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["pause_project", "write", "natural-language"],
        setup_commands=[("create_project", {"name": "Alpha"})],
        turns=[
            Turn(
                user_message="hold off on project p-1, pause it for now",
                expected_tools=[
                    ExpectedTool(name="pause_project", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # resume_project — TRIVIAL / EASY
    # -----------------------------------------------------------------------
    TestCase(
        id="proj-resume-direct",
        description="Resume a project by ID",
        category="projects",
        difficulty=Difficulty.TRIVIAL,
        tags=["resume_project", "write"],
        setup_commands=[
            ("create_project", {"name": "Paused Project"}),
            ("pause_project", {"project_id": "p-1"}),
        ],
        turns=[
            Turn(
                user_message="resume project p-1",
                expected_tools=[
                    ExpectedTool(name="resume_project", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-resume-natural",
        description="Resume with casual language",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["resume_project", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "Frozen"}),
            ("pause_project", {"project_id": "p-1"}),
        ],
        turns=[
            Turn(
                user_message="unpause project p-1, let's get it going again",
                expected_tools=[
                    ExpectedTool(name="resume_project", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # edit_project — EASY / MEDIUM / HARD
    # -----------------------------------------------------------------------
    TestCase(
        id="proj-edit-rename",
        description="Rename a project",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["edit_project", "write"],
        setup_commands=[("create_project", {"name": "Alpha"})],
        turns=[
            Turn(
                user_message="change project p-1 name to Beta",
                expected_tools=[
                    ExpectedTool(
                        name="edit_project",
                        args={"project_id": "p-1", "name": "Beta"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-edit-rename-natural",
        description="Rename a project with casual phrasing",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_project", "write", "natural-language"],
        setup_commands=[("create_project", {"name": "Old Name"})],
        turns=[
            Turn(
                user_message="rename project p-1 to New Name",
                expected_tools=[
                    ExpectedTool(
                        name="edit_project",
                        args={"project_id": "p-1", "name": "New Name"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-edit-weight",
        description="Change a project's credit weight",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_project", "write"],
        setup_commands=[("create_project", {"name": "Weighted"})],
        turns=[
            Turn(
                user_message="set credit weight for project p-1 to 3.0",
                expected_tools=[
                    ExpectedTool(
                        name="edit_project",
                        args={"project_id": "p-1", "credit_weight": 3.0},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-edit-max-agents",
        description="Update max concurrent agents for a project",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_project", "write"],
        setup_commands=[("create_project", {"name": "ScaleUp"})],
        turns=[
            Turn(
                user_message="increase the max concurrent agents on p-1 to 5",
                expected_tools=[
                    ExpectedTool(
                        name="edit_project",
                        args={"project_id": "p-1", "max_concurrent_agents": 5},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-edit-budget",
        description="Set a token budget limit on a project",
        category="projects",
        difficulty=Difficulty.HARD,
        tags=["edit_project", "write", "multi-arg"],
        setup_commands=[("create_project", {"name": "Budgeted"})],
        turns=[
            Turn(
                user_message="set a 500000 token budget on project p-1",
                expected_tools=[
                    ExpectedTool(
                        name="edit_project",
                        args={"project_id": "p-1", "budget_limit": 500000},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # get_project_channels — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="proj-channels-direct",
        description="Get channels for a project by ID",
        category="projects",
        difficulty=Difficulty.EASY,
        tags=["get_project_channels", "read"],
        setup_commands=[("create_project", {"name": "ChannelTest"})],
        turns=[
            Turn(
                user_message="what channel is project p-1 using?",
                expected_tools=[
                    ExpectedTool(
                        name="get_project_channels",
                        args={"project_id": "p-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-channels-natural",
        description="Ask about Discord channels for a project",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["get_project_channels", "read", "natural-language"],
        setup_commands=[("create_project", {"name": "Showcase"})],
        turns=[
            Turn(
                user_message="show me the Discord channel linked to p-1",
                expected_tools=[
                    ExpectedTool(
                        name="get_project_channels",
                        args={"project_id": "p-1"},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # get_project_for_channel — MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="proj-for-channel-direct",
        description="Find which project owns a channel ID",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["get_project_for_channel", "read"],
        turns=[
            Turn(
                user_message="what project is channel 1234567890 linked to?",
                expected_tools=[
                    ExpectedTool(
                        name="get_project_for_channel",
                        args={"channel_id": "1234567890"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-for-channel-which",
        description="Ask which project a channel belongs to",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["get_project_for_channel", "read", "natural-language"],
        turns=[
            Turn(
                user_message="which project uses channel 9876543210?",
                expected_tools=[
                    ExpectedTool(
                        name="get_project_for_channel",
                        args={"channel_id": "9876543210"},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # delete_project — MEDIUM / HARD
    # -----------------------------------------------------------------------
    TestCase(
        id="proj-delete-by-id",
        description="Delete a project by ID",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["delete_project", "write", "destructive"],
        setup_commands=[("create_project", {"name": "Disposable"})],
        turns=[
            Turn(
                user_message="delete project p-1",
                expected_tools=[
                    ExpectedTool(
                        name="delete_project",
                        args={"project_id": "p-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-delete-with-archive",
        description="Delete a project and archive its channels",
        category="projects",
        difficulty=Difficulty.HARD,
        tags=["delete_project", "write", "destructive", "multi-arg"],
        setup_commands=[("create_project", {"name": "Archivable"})],
        turns=[
            Turn(
                user_message="delete project p-1 and archive its Discord channels",
                expected_tools=[
                    ExpectedTool(
                        name="delete_project",
                        args={"project_id": "p-1", "archive_channels": True},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="proj-delete-natural",
        description="Delete a project with natural language",
        category="projects",
        difficulty=Difficulty.MEDIUM,
        tags=["delete_project", "write", "destructive", "natural-language"],
        setup_commands=[("create_project", {"name": "Temporary"})],
        turns=[
            Turn(
                user_message="remove project p-1, we don't need it anymore",
                expected_tools=[
                    ExpectedTool(
                        name="delete_project",
                        args={"project_id": "p-1"},
                    ),
                ],
            ),
        ],
    ),
]
