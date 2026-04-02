"""Test cases for automation tools (supervisor evaluation).

Covers: save_rule (create automation), browse_rules, delete_rule,
        list_hooks (read-only), list_hook_runs, fire_hook.

Rules are the primary interface for creating automation. Hooks are internal
execution artifacts generated from rules — create_hook, edit_hook, and
delete_hook have been removed in favor of the rule-based interface.

20 test cases: updated for unified rules-as-first-class model.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- list_hooks (read-only inspection) ---
    TestCase(
        id="hooks-list-all",
        description="List all hooks across all projects",
        category="hooks",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_hooks"],
        turns=[
            Turn(
                user_message="list all hooks",
                expected_tools=[ExpectedTool(name="list_hooks")],
            ),
        ],
    ),
    TestCase(
        id="hooks-list-by-project",
        description="List hooks filtered by project",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["list_hooks"],
        turns=[
            Turn(
                user_message="show hooks for project p-1",
                expected_tools=[
                    ExpectedTool(name="list_hooks", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-list-natural",
        description="List hooks with natural phrasing",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["list_hooks", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what hooks are set up?",
                expected_tools=[ExpectedTool(name="list_hooks")],
            ),
        ],
    ),
    # --- save_rule (create automation via rules) ---
    TestCase(
        id="hooks-create-periodic-rule",
        description="Create a periodic automation via save_rule",
        category="hooks",
        difficulty=Difficulty.HARD,
        tags=["save_rule", "periodic"],
        turns=[
            Turn(
                user_message=(
                    "create an automation for project p-1 that checks if deployment "
                    "is healthy every 30 minutes"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="save_rule",
                        args={
                            "type": "active",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-create-event-rule",
        description="Create an event-triggered automation via save_rule",
        category="hooks",
        difficulty=Difficulty.HARD,
        tags=["save_rule", "event"],
        turns=[
            Turn(
                user_message=(
                    "create a rule for project p-2 that reviews completed task "
                    "results when a task finishes"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="save_rule",
                        args={
                            "type": "active",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-create-health-monitor-rule",
        description="Create a health monitoring automation via save_rule",
        category="hooks",
        difficulty=Difficulty.HARD,
        tags=["save_rule", "periodic"],
        turns=[
            Turn(
                user_message=(
                    "set up a rule for project p-1 that monitors the health endpoint "
                    "at localhost:8080/health every 15 minutes and reports issues"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="save_rule",
                        args={
                            "type": "active",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-create-natural-rule",
        description="Create an automation with casual natural language",
        category="hooks",
        difficulty=Difficulty.HARD,
        tags=["save_rule", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "I want something that periodically runs and checks if all tests pass"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="save_rule",
                        args={
                            "type": "active",
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- browse_rules ---
    TestCase(
        id="rules-browse-all",
        description="Browse all rules for a project",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["browse_rules"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show me all the automation rules",
                expected_tools=[ExpectedTool(name="browse_rules")],
            ),
        ],
    ),
    TestCase(
        id="rules-browse-by-project",
        description="Browse rules filtered by project",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["browse_rules"],
        turns=[
            Turn(
                user_message="list rules for project p-2",
                expected_tools=[
                    ExpectedTool(name="browse_rules", args={"project_id": "p-2"}),
                ],
            ),
        ],
    ),
    # --- delete_rule ---
    TestCase(
        id="rules-delete-explicit",
        description="Delete a rule by ID",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["delete_rule"],
        turns=[
            Turn(
                user_message="delete rule rule-deploy-check",
                expected_tools=[
                    ExpectedTool(name="delete_rule", args={"id": "rule-deploy-check"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="rules-delete-natural",
        description="Remove a rule using natural phrasing",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["delete_rule", "natural-language"],
        turns=[
            Turn(
                user_message="remove the health-monitor automation rule",
                expected_tools=[ExpectedTool(name="delete_rule")],
            ),
        ],
    ),
    # --- list_hook_runs ---
    TestCase(
        id="hooks-list-runs",
        description="Show run history for a hook",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["list_hook_runs"],
        turns=[
            Turn(
                user_message="show runs for hook h-1",
                expected_tools=[
                    ExpectedTool(name="list_hook_runs", args={"hook_id": "h-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-list-runs-natural",
        description="Ask about hook execution history with natural language",
        category="hooks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_hook_runs", "natural-language"],
        turns=[
            Turn(
                user_message="how many times has hook h-2 run recently?",
                expected_tools=[
                    ExpectedTool(name="list_hook_runs", args={"hook_id": "h-2"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-list-runs-with-limit",
        description="Show limited number of hook runs",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["list_hook_runs"],
        turns=[
            Turn(
                user_message="show the last 5 runs for hook h-1",
                expected_tools=[
                    ExpectedTool(
                        name="list_hook_runs",
                        args={"hook_id": "h-1", "limit": 5},
                    ),
                ],
            ),
        ],
    ),
    # --- fire_hook ---
    TestCase(
        id="hooks-fire-explicit",
        description="Manually trigger a hook by ID",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["fire_hook"],
        turns=[
            Turn(
                user_message="fire hook h-1 now",
                expected_tools=[
                    ExpectedTool(name="fire_hook", args={"hook_id": "h-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-fire-trigger-phrasing",
        description="Manually trigger a hook using 'trigger' phrasing",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["fire_hook", "natural-language"],
        turns=[
            Turn(
                user_message="trigger hook h-2 manually",
                expected_tools=[
                    ExpectedTool(name="fire_hook", args={"hook_id": "h-2"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-fire-run-phrasing",
        description="Fire a hook using 'run' phrasing",
        category="hooks",
        difficulty=Difficulty.MEDIUM,
        tags=["fire_hook", "natural-language"],
        turns=[
            Turn(
                user_message="run the deploy-check hook right now",
                expected_tools=[ExpectedTool(name="fire_hook")],
            ),
        ],
    ),
    # --- save_rule (passive rules) ---
    TestCase(
        id="rules-create-passive",
        description="Create a passive rule that influences reasoning",
        category="hooks",
        difficulty=Difficulty.MEDIUM,
        tags=["save_rule", "passive"],
        turns=[
            Turn(
                user_message=(
                    "create a passive rule that says 'When reviewing PRs, always "
                    "check for SQL injection vulnerabilities'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="save_rule",
                        args={
                            "type": "passive",
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- load_rule ---
    TestCase(
        id="rules-load-detail",
        description="Load a specific rule's full content",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["load_rule"],
        turns=[
            Turn(
                user_message="show me the details of rule rule-deploy-check",
                expected_tools=[
                    ExpectedTool(name="load_rule", args={"id": "rule-deploy-check"}),
                ],
            ),
        ],
    ),
]
