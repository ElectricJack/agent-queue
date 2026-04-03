"""Test cases for agent management tools (supervisor evaluation).

Covers: list_agents, create_agent, edit_agent, pause_agent, resume_agent, delete_agent

20 test cases: verified against current supervisor-based architecture.
Agents are worker entities managed by the Supervisor and assigned tasks from the queue.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # -----------------------------------------------------------------------
    # list_agents — TRIVIAL / EASY
    # -----------------------------------------------------------------------
    TestCase(
        id="agent-list-trivial",
        description="Direct 'list agents' command",
        category="agents",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_agents", "read"],
        turns=[
            Turn(
                user_message="list all agents",
                expected_tools=[ExpectedTool(name="list_agents")],
            ),
        ],
    ),
    TestCase(
        id="agent-list-question",
        description="Ask about agents as a question",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["list_agents", "read", "natural-language"],
        turns=[
            Turn(
                user_message="what agents are available?",
                expected_tools=[ExpectedTool(name="list_agents")],
            ),
        ],
    ),
    TestCase(
        id="agent-list-how-many",
        description="Ask how many agents are running",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["list_agents", "read", "natural-language"],
        turns=[
            Turn(
                user_message="how many agents are running?",
                expected_tools=[ExpectedTool(name="list_agents")],
            ),
        ],
    ),
    TestCase(
        id="agent-list-show",
        description="Show agents with casual phrasing",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["list_agents", "read", "natural-language"],
        turns=[
            Turn(
                user_message="show me the agents",
                expected_tools=[ExpectedTool(name="list_agents")],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # create_agent — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="agent-create-no-args",
        description="Create an agent with auto-generated name",
        category="agents",
        difficulty=Difficulty.TRIVIAL,
        tags=["create_agent", "write"],
        turns=[
            Turn(
                user_message="create a new agent",
                expected_tools=[ExpectedTool(name="create_agent")],
            ),
        ],
    ),
    TestCase(
        id="agent-create-with-name",
        description="Create an agent with a specific name",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["create_agent", "write"],
        turns=[
            Turn(
                user_message="create an agent called builder",
                expected_tools=[
                    ExpectedTool(name="create_agent", args={"name": "builder"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-create-spin-up",
        description="Spin up another agent (casual language)",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["create_agent", "write", "natural-language"],
        turns=[
            Turn(
                user_message="spin up another agent",
                expected_tools=[ExpectedTool(name="create_agent")],
            ),
        ],
    ),
    TestCase(
        id="agent-create-with-type",
        description="Create an agent with a specific type",
        category="agents",
        difficulty=Difficulty.MEDIUM,
        tags=["create_agent", "write", "multi-arg"],
        turns=[
            Turn(
                user_message="create a new codex agent named analyzer",
                expected_tools=[
                    ExpectedTool(
                        name="create_agent",
                        args={"name": "analyzer", "agent_type": "codex"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-create-add-workers",
        description="Add more workers (natural language for creating agents)",
        category="agents",
        difficulty=Difficulty.MEDIUM,
        tags=["create_agent", "write", "natural-language"],
        turns=[
            Turn(
                user_message="I need more workers, add a new agent",
                expected_tools=[ExpectedTool(name="create_agent")],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # edit_agent — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="agent-edit-rename",
        description="Rename an agent",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["edit_agent", "write"],
        setup_commands=[("create_agent", {"name": "old-name"})],
        turns=[
            Turn(
                user_message="rename agent a-1 to builder",
                expected_tools=[
                    ExpectedTool(
                        name="edit_agent",
                        args={"agent_id": "a-1", "name": "builder"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-edit-change-type",
        description="Change an agent's type",
        category="agents",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_agent", "write"],
        setup_commands=[("create_agent", {"name": "flex"})],
        turns=[
            Turn(
                user_message="change agent a-1 type to aider",
                expected_tools=[
                    ExpectedTool(
                        name="edit_agent",
                        args={"agent_id": "a-1", "agent_type": "aider"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-edit-rename-natural",
        description="Rename an agent with natural language",
        category="agents",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_agent", "write", "natural-language"],
        setup_commands=[("create_agent", {"name": "worker-1"})],
        turns=[
            Turn(
                user_message="call agent a-1 'frontend-specialist' instead",
                expected_tools=[
                    ExpectedTool(
                        name="edit_agent",
                        args={"agent_id": "a-1", "name": "frontend-specialist"},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # pause_agent — TRIVIAL / EASY
    # -----------------------------------------------------------------------
    TestCase(
        id="agent-pause-direct",
        description="Pause an agent by ID",
        category="agents",
        difficulty=Difficulty.TRIVIAL,
        tags=["pause_agent", "write"],
        setup_commands=[("create_agent", {"name": "pausable"})],
        turns=[
            Turn(
                user_message="pause agent a-1",
                expected_tools=[
                    ExpectedTool(name="pause_agent", args={"agent_id": "a-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-pause-natural",
        description="Pause an agent with natural language",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["pause_agent", "write", "natural-language"],
        setup_commands=[("create_agent", {"name": "active"})],
        turns=[
            Turn(
                user_message="hold agent a-1 from picking up new tasks",
                expected_tools=[
                    ExpectedTool(name="pause_agent", args={"agent_id": "a-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # resume_agent — TRIVIAL / EASY
    # -----------------------------------------------------------------------
    TestCase(
        id="agent-resume-direct",
        description="Resume an agent by ID",
        category="agents",
        difficulty=Difficulty.TRIVIAL,
        tags=["resume_agent", "write"],
        setup_commands=[
            ("create_agent", {"name": "paused-agent"}),
            ("pause_agent", {"agent_id": "a-1"}),
        ],
        turns=[
            Turn(
                user_message="resume agent a-1",
                expected_tools=[
                    ExpectedTool(name="resume_agent", args={"agent_id": "a-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-resume-unpause",
        description="Resume using 'unpause' wording",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["resume_agent", "write", "natural-language"],
        setup_commands=[
            ("create_agent", {"name": "waiting"}),
            ("pause_agent", {"agent_id": "a-1"}),
        ],
        turns=[
            Turn(
                user_message="unpause agent a-1",
                expected_tools=[
                    ExpectedTool(name="resume_agent", args={"agent_id": "a-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-resume-start-again",
        description="Resume agent with natural language",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["resume_agent", "write", "natural-language"],
        setup_commands=[
            ("create_agent", {"name": "resting"}),
            ("pause_agent", {"agent_id": "a-1"}),
        ],
        turns=[
            Turn(
                user_message="let agent a-1 start receiving tasks again",
                expected_tools=[
                    ExpectedTool(name="resume_agent", args={"agent_id": "a-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # delete_agent — EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="agent-delete-direct",
        description="Delete an agent by ID",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["delete_agent", "write", "destructive"],
        setup_commands=[("create_agent", {"name": "disposable"})],
        turns=[
            Turn(
                user_message="delete agent a-1",
                expected_tools=[
                    ExpectedTool(name="delete_agent", args={"agent_id": "a-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-delete-remove",
        description="Remove an agent with 'remove' wording",
        category="agents",
        difficulty=Difficulty.EASY,
        tags=["delete_agent", "write", "destructive", "natural-language"],
        setup_commands=[("create_agent", {"name": "extra"})],
        turns=[
            Turn(
                user_message="remove agent a-1",
                expected_tools=[
                    ExpectedTool(name="delete_agent", args={"agent_id": "a-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="agent-delete-natural",
        description="Delete agent with casual language",
        category="agents",
        difficulty=Difficulty.MEDIUM,
        tags=["delete_agent", "write", "destructive", "natural-language"],
        setup_commands=[("create_agent", {"name": "unnecessary"})],
        turns=[
            Turn(
                user_message="get rid of agent a-1, we don't need that many",
                expected_tools=[
                    ExpectedTool(name="delete_agent", args={"agent_id": "a-1"}),
                ],
            ),
        ],
    ),
]
