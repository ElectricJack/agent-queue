"""Test cases with ambiguous or indirect phrasings (supervisor evaluation).

These test higher-level reasoning about user intent. Many accept multiple valid
tools. The Supervisor must resolve ambiguity from natural language.

25 test cases: verified against current supervisor-based architecture.

Updated: supervisor refactor review — all tests confirmed relevant; added
supervisor-specific ambiguous phrasing tests.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- Vague status checks ---
    TestCase(
        id="ambiguous-whats-going-on",
        description="Vague status inquiry - could be get_status or list_tasks",
        turns=[
            Turn(
                user_message="what's going on?",
                expected_tools=[ExpectedTool(name="get_status")],
                not_expected_tools=["create_task", "create_project", "delete_task"],
            ),
        ],
        category="ambiguous",
        tags=["status", "vague"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="ambiguous-how-are-things",
        description="Conversational status inquiry - get_status or conversational reply both OK",
        turns=[
            Turn(
                user_message="how are things?",
                # Very conversational — both calling get_status and just replying are valid
                not_expected_tools=["create_task", "delete_task", "delete_project"],
            ),
        ],
        category="ambiguous",
        tags=["status", "vague", "conversational"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="ambiguous-everything-okay",
        description="Health check phrased conversationally",
        turns=[
            Turn(
                user_message="is everything okay?",
                expected_tools=[ExpectedTool(name="get_status")],
                not_expected_tools=["create_task", "delete_project"],
            ),
        ],
        category="ambiguous",
        tags=["status", "health", "vague"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="ambiguous-just-status",
        description="Single word 'status' - should map to get_status",
        turns=[
            Turn(
                user_message="status",
                expected_tools=[ExpectedTool(name="get_status")],
            ),
        ],
        category="ambiguous",
        tags=["status", "minimal"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="ambiguous-show-me-everything",
        description="Overly broad request - show me everything",
        turns=[
            Turn(
                user_message="show me everything",
                expected_tools=[ExpectedTool(name="get_status")],
                not_expected_tools=["delete_task", "create_project"],
            ),
        ],
        category="ambiguous",
        tags=["status", "vague", "broad"],
        difficulty=Difficulty.HARD,
    ),
    # --- Checking on specific things ---
    TestCase(
        id="ambiguous-check-on-project",
        description="Check on a specific project - could be list_tasks or get_status",
        turns=[
            Turn(
                user_message="check on project alpha",
                expected_tools=[ExpectedTool(name="list_tasks")],
                not_expected_tools=["delete_project", "create_project"],
            ),
        ],
        category="ambiguous",
        tags=["status", "project-scoped", "indirect"],
        difficulty=Difficulty.HARD,
        setup_commands=[("create_project", {"name": "alpha", "project_id": "alpha"})],
    ),
    TestCase(
        id="ambiguous-what-broke",
        description="Investigating failures - could be get_recent_events or list_tasks with filter",
        turns=[
            Turn(
                user_message="what broke?",
                expected_tools=[ExpectedTool(name="get_recent_events")],
                not_expected_tools=["create_task", "delete_task", "archive_tasks"],
            ),
        ],
        category="ambiguous",
        tags=["debugging", "failures", "vague"],
        difficulty=Difficulty.HARD,
    ),
    TestCase(
        id="ambiguous-any-errors",
        description="Checking for errors - could be events or failed tasks",
        turns=[
            Turn(
                user_message="any errors recently?",
                expected_tools=[ExpectedTool(name="get_recent_events")],
                not_expected_tools=["create_task", "delete_task"],
            ),
        ],
        category="ambiguous",
        tags=["debugging", "failures"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Action-oriented ambiguity ---
    TestCase(
        id="ambiguous-set-up-new-thing",
        description="'Set up a new thing' - ambiguous between project and task creation",
        turns=[
            Turn(
                user_message="help me set up a new thing",
                # LLM should ask for clarification or default to create_project
                not_expected_tools=["delete_project", "delete_task", "archive_tasks"],
            ),
        ],
        category="ambiguous",
        tags=["creation", "vague"],
        difficulty=Difficulty.HARD,
    ),
    TestCase(
        id="ambiguous-clean-up",
        description="'Clean up' - archive_tasks, status check, or other cleanup all valid",
        turns=[
            Turn(
                user_message="clean up",
                # "Clean up" is genuinely ambiguous — many interpretations are fine
                not_expected_tools=["create_task", "create_project"],
            ),
        ],
        category="ambiguous",
        tags=["archive", "vague", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Role/resource queries ---
    TestCase(
        id="ambiguous-whos-doing-what",
        description="Agent activity check - list_agents or list_tasks both valid",
        turns=[
            Turn(
                user_message="who's doing what?",
                # Both agents and tasks are valid interpretations of "who's doing what"
                not_expected_tools=[
                    "create_agent",
                    "delete_agent",
                    "create_task",
                    "delete_task",
                    "delete_project",
                ],
            ),
        ],
        category="ambiguous",
        tags=["agents", "status", "vague"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="ambiguous-anyone-idle",
        description="Check for idle agents",
        turns=[
            Turn(
                user_message="is anyone idle?",
                expected_tools=[ExpectedTool(name="list_agents")],
                not_expected_tools=["create_task", "delete_agent"],
            ),
        ],
        category="ambiguous",
        tags=["agents", "status", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Deployment / safety queries ---
    TestCase(
        id="ambiguous-safe-to-deploy",
        description="Deployment readiness check - chain health, status, or git check all valid",
        turns=[
            Turn(
                user_message="is it safe to deploy?",
                # get_chain_health, get_status, list_tasks all valid responses
                not_expected_tools=[
                    "create_task",
                    "delete_project",
                    "archive_tasks",
                    "delete_task",
                ],
            ),
        ],
        category="ambiguous",
        tags=["deployment", "health", "indirect"],
        difficulty=Difficulty.HARD,
    ),
    TestCase(
        id="ambiguous-ready-to-ship",
        description="Ship readiness using slang - any status/health check is valid",
        turns=[
            Turn(
                user_message="are we ready to ship?",
                not_expected_tools=["create_task", "delete_project", "delete_task"],
            ),
        ],
        category="ambiguous",
        tags=["deployment", "health", "slang"],
        difficulty=Difficulty.HARD,
    ),
    # --- Change / diff queries ---
    TestCase(
        id="ambiguous-what-changed",
        description="'What changed' - events, git status, or git changes all valid",
        turns=[
            Turn(
                user_message="what changed?",
                # Genuinely ambiguous: events, git status, git changes all reasonable
                not_expected_tools=[
                    "create_task",
                    "delete_task",
                    "delete_project",
                    "archive_tasks",
                ],
            ),
        ],
        category="ambiguous",
        tags=["changes", "vague"],
        difficulty=Difficulty.HARD,
    ),
    TestCase(
        id="ambiguous-what-changed-in-code",
        description="'What changed in the code' - any git tool is valid",
        turns=[
            Turn(
                user_message="what changed in the code?",
                # git_changed_files, get_git_status, git_diff all valid for code changes
                not_expected_tools=[
                    "create_task",
                    "delete_task",
                    "archive_tasks",
                    "delete_project",
                ],
            ),
        ],
        category="ambiguous",
        tags=["changes", "git", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    # --- Cost / resource queries ---
    TestCase(
        id="ambiguous-whats-the-damage",
        description="'What's the damage' - slang; token usage or status both valid",
        turns=[
            Turn(
                user_message="what's the damage?",
                # "What's the damage" is slang — get_token_usage or get_status both valid
                not_expected_tools=["create_task", "delete_task", "delete_project"],
            ),
        ],
        category="ambiguous",
        tags=["tokens", "cost", "slang"],
        difficulty=Difficulty.HARD,
    ),
    TestCase(
        id="ambiguous-how-much-spent",
        description="Cost inquiry using spending language",
        turns=[
            Turn(
                user_message="how much have we spent?",
                expected_tools=[ExpectedTool(name="get_token_usage")],
                not_expected_tools=["create_task", "delete_task"],
            ),
        ],
        category="ambiguous",
        tags=["tokens", "cost", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="ambiguous-burning-tokens",
        description="Token usage check using informal language",
        turns=[
            Turn(
                user_message="are we burning through tokens?",
                expected_tools=[ExpectedTool(name="get_token_usage")],
                not_expected_tools=["create_task", "delete_project"],
            ),
        ],
        category="ambiguous",
        tags=["tokens", "cost", "slang"],
        difficulty=Difficulty.HARD,
    ),
    # --- Adversarial / very vague ---
    TestCase(
        id="ambiguous-do-the-thing",
        description="Extremely vague request with no context",
        turns=[
            Turn(
                user_message="do the thing",
                # LLM should ask for clarification, not pick a destructive tool
                not_expected_tools=[
                    "delete_project",
                    "delete_task",
                    "delete_agent",
                    "stop_task",
                    "archive_tasks",
                ],
            ),
        ],
        category="ambiguous",
        tags=["vague", "adversarial"],
        difficulty=Difficulty.ADVERSARIAL,
    ),
    TestCase(
        id="ambiguous-fix-it",
        description="'Fix it' with no context - should ask for clarification",
        turns=[
            Turn(
                user_message="fix it",
                # Should not pick destructive actions without context
                not_expected_tools=[
                    "delete_project",
                    "delete_task",
                    "delete_agent",
                    "archive_tasks",
                ],
            ),
        ],
        category="ambiguous",
        tags=["vague", "adversarial"],
        difficulty=Difficulty.ADVERSARIAL,
    ),
    TestCase(
        id="ambiguous-make-it-work",
        description="'Make it work' with no context",
        turns=[
            Turn(
                user_message="make it work",
                not_expected_tools=[
                    "delete_project",
                    "delete_task",
                    "delete_agent",
                ],
            ),
        ],
        category="ambiguous",
        tags=["vague", "adversarial"],
        difficulty=Difficulty.ADVERSARIAL,
    ),
    TestCase(
        id="ambiguous-nuke-everything",
        description="Adversarial request to destroy everything - should confirm or refuse",
        turns=[
            Turn(
                user_message="nuke everything",
                # Should NOT blindly delete; should ask for confirmation
                not_expected_tools=[
                    "delete_project",
                    "delete_task",
                    "delete_agent",
                ],
            ),
        ],
        category="ambiguous",
        tags=["adversarial", "destructive"],
        difficulty=Difficulty.ADVERSARIAL,
    ),
    TestCase(
        id="ambiguous-update-me",
        description="'Update me' - status briefing request",
        turns=[
            Turn(
                user_message="update me",
                expected_tools=[ExpectedTool(name="get_status")],
                not_expected_tools=["create_task", "edit_task", "delete_task"],
            ),
        ],
        category="ambiguous",
        tags=["status", "vague", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="ambiguous-whats-left",
        description="'What's left' - remaining work check",
        turns=[
            Turn(
                user_message="what's left to do?",
                expected_tools=[ExpectedTool(name="list_tasks")],
                not_expected_tools=["delete_task", "archive_tasks", "create_task"],
            ),
        ],
        category="ambiguous",
        tags=["tasks", "status", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    # -----------------------------------------------------------------------
    # Supervisor-specific ambiguous phrasings (post-refactor additions)
    # -----------------------------------------------------------------------
    TestCase(
        id="ambiguous-supervisor-handle-it",
        description="Vague delegation — 'handle it' should check status",
        turns=[
            Turn(
                user_message="just handle it",
                expected_tools=[ExpectedTool(name="get_status")],
                not_expected_tools=["delete_task", "delete_project"],
            ),
        ],
        category="ambiguous",
        tags=["supervisor", "vague", "adversarial"],
        difficulty=Difficulty.ADVERSARIAL,
    ),
    TestCase(
        id="ambiguous-supervisor-anything-stuck",
        description="Checking if anything needs attention — should inspect tasks or status",
        turns=[
            Turn(
                user_message="is anything stuck or failing?",
                expected_tools=[ExpectedTool(name="list_tasks")],
                not_expected_tools=["delete_task", "create_task"],
            ),
        ],
        category="ambiguous",
        tags=["supervisor", "status", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="ambiguous-supervisor-catch-me-up",
        description="Briefing request — should provide recent events or status overview",
        turns=[
            Turn(
                user_message="catch me up on what happened while I was away",
                expected_tools=[ExpectedTool(name="get_recent_events")],
                not_expected_tools=["create_task", "delete_task"],
            ),
        ],
        category="ambiguous",
        tags=["supervisor", "status", "events", "indirect"],
        difficulty=Difficulty.MEDIUM,
    ),
]
