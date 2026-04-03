"""Test cases for task lifecycle/workflow tools (supervisor evaluation).

Covers: stop_task, restart_task, reopen_with_feedback, approve_task, skip_task,
approve_plan, reject_plan, delete_plan.

24 test cases: verified against current supervisor-based architecture.
These test the core task state machine that the Supervisor controls.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # -----------------------------------------------------------------------
    # stop_task — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="workflow-stop-direct",
        description="Stop a task by ID",
        category="task_workflow",
        difficulty=Difficulty.TRIVIAL,
        tags=["stop_task", "write"],
        setup_commands=[
            ("create_project", {"name": "StopProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Running Task", "description": "In progress"},
            ),
        ],
        turns=[
            Turn(
                user_message="stop task t-1",
                expected_tools=[
                    ExpectedTool(name="stop_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-stop-cancel",
        description="Cancel a task using 'cancel' wording",
        category="task_workflow",
        difficulty=Difficulty.EASY,
        tags=["stop_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "CancelProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Unneeded", "description": "No longer needed"},
            ),
        ],
        turns=[
            Turn(
                user_message="cancel task t-1, it's no longer needed",
                expected_tools=[
                    ExpectedTool(name="stop_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-stop-abort",
        description="Abort a task",
        category="task_workflow",
        difficulty=Difficulty.EASY,
        tags=["stop_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "AbortProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Wrong approach", "description": "Bad idea"},
            ),
        ],
        turns=[
            Turn(
                user_message="abort t-1, the approach is all wrong",
                expected_tools=[
                    ExpectedTool(name="stop_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-stop-kill",
        description="Kill a task using informal language",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["stop_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "KillProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Runaway", "description": "Out of control"},
            ),
        ],
        turns=[
            Turn(
                user_message="kill t-1, it's stuck in a loop",
                expected_tools=[
                    ExpectedTool(name="stop_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # restart_task — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="workflow-restart-direct",
        description="Restart a task by ID",
        category="task_workflow",
        difficulty=Difficulty.TRIVIAL,
        tags=["restart_task", "write"],
        setup_commands=[
            ("create_project", {"name": "RestartProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Failed Task", "description": "It failed"},
            ),
        ],
        turns=[
            Turn(
                user_message="restart task t-1",
                expected_tools=[
                    ExpectedTool(name="restart_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-restart-failed",
        description="Restart a failed task",
        category="task_workflow",
        difficulty=Difficulty.EASY,
        tags=["restart_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "RetryProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Retry This", "description": "Needs retry"},
            ),
        ],
        turns=[
            Turn(
                user_message="restart the failed task t-1",
                expected_tools=[
                    ExpectedTool(name="restart_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-restart-retry",
        description="Retry a task using 'retry' wording",
        category="task_workflow",
        difficulty=Difficulty.EASY,
        tags=["restart_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "RetryProj2"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Another Fail", "description": "Failed again"},
            ),
        ],
        turns=[
            Turn(
                user_message="retry t-1",
                expected_tools=[
                    ExpectedTool(name="restart_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-restart-run-again",
        description="Ask to run a task again",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["restart_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "RerunProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Redo Task", "description": "Run it again"},
            ),
        ],
        turns=[
            Turn(
                user_message="can you run t-1 again? it didn't work the first time",
                expected_tools=[
                    ExpectedTool(name="restart_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # reopen_with_feedback — EASY / MEDIUM / HARD
    # -----------------------------------------------------------------------
    TestCase(
        id="workflow-reopen-direct",
        description="Reopen a task with explicit feedback",
        category="task_workflow",
        difficulty=Difficulty.EASY,
        tags=["reopen_with_feedback", "write"],
        setup_commands=[
            ("create_project", {"name": "ReopenProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "QA Failed", "description": "Needs fix"},
            ),
        ],
        turns=[
            Turn(
                user_message="reopen t-1 with feedback: the tests are still failing",
                expected_tools=[
                    ExpectedTool(
                        name="reopen_with_feedback",
                        args={
                            "task_id": "t-1",
                            "feedback": "the tests are still failing",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-reopen-detailed-feedback",
        description="Reopen with detailed QA feedback",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["reopen_with_feedback", "write"],
        setup_commands=[
            ("create_project", {"name": "QAProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Login Fix", "description": "Fix login"},
            ),
        ],
        turns=[
            Turn(
                user_message=(
                    "reopen task t-1, the login page still shows a 500 error "
                    "when using special characters in the password"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="reopen_with_feedback",
                        args={"task_id": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-reopen-send-back",
        description="'send back' phrasing for reopen with feedback",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["reopen_with_feedback", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "SendBack"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Incomplete", "description": "Not done"},
            ),
        ],
        turns=[
            Turn(
                user_message=(
                    "send t-1 back for rework -- the API response format doesn't match the spec"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="reopen_with_feedback",
                        args={"task_id": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-reopen-needs-fix",
        description="Natural language indicating a fix is needed",
        category="task_workflow",
        difficulty=Difficulty.HARD,
        tags=["reopen_with_feedback", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "FixAgain"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Broken Again", "description": "Still broken"},
            ),
        ],
        turns=[
            Turn(
                user_message=(
                    "t-1 isn't right, the pagination breaks when there are "
                    "more than 100 items. send it back for another pass"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="reopen_with_feedback",
                        args={"task_id": "t-1"},
                    ),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # approve_task — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="workflow-approve-direct",
        description="Approve a task by ID",
        category="task_workflow",
        difficulty=Difficulty.TRIVIAL,
        tags=["approve_task", "write"],
        setup_commands=[
            ("create_project", {"name": "ApproveProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Pending", "description": "Awaiting approval"},
            ),
        ],
        turns=[
            Turn(
                user_message="approve task t-1",
                expected_tools=[
                    ExpectedTool(name="approve_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-approve-lgtm",
        description="Approve using LGTM shorthand",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["approve_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "LGTMProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Ship It", "description": "Ready to ship"},
            ),
        ],
        turns=[
            Turn(
                user_message="LGTM on t-1, ship it",
                expected_tools=[
                    ExpectedTool(name="approve_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-approve-looks-good",
        description="Approve using 'looks good' phrasing",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["approve_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "GoodProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Reviewed", "description": "Reviewed task"},
            ),
        ],
        turns=[
            Turn(
                user_message="t-1 looks good, go ahead and approve it",
                expected_tools=[
                    ExpectedTool(name="approve_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # skip_task — TRIVIAL / EASY / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="workflow-skip-direct",
        description="Skip a task by ID",
        category="task_workflow",
        difficulty=Difficulty.TRIVIAL,
        tags=["skip_task", "write"],
        setup_commands=[
            ("create_project", {"name": "SkipProj"}),
            ("create_task", {"project_id": "p-1", "title": "Blocked Task", "description": "Stuck"}),
        ],
        turns=[
            Turn(
                user_message="skip task t-1",
                expected_tools=[
                    ExpectedTool(name="skip_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-skip-unblock",
        description="Skip a task to unblock its dependents",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["skip_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "UnblockProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Blocker", "description": "Blocking others"},
            ),
        ],
        turns=[
            Turn(
                user_message="skip t-1 so its downstream tasks can proceed",
                expected_tools=[
                    ExpectedTool(name="skip_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-skip-not-needed",
        description="Skip a task that is no longer needed",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["skip_task", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "SkipNeed"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Optional", "description": "Optional work"},
            ),
        ],
        turns=[
            Turn(
                user_message="mark t-1 as skipped, we handled it manually",
                expected_tools=[
                    ExpectedTool(name="skip_task", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # approve_plan — TRIVIAL / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="workflow-approve-plan-direct",
        description="Approve a plan by task ID",
        category="task_workflow",
        difficulty=Difficulty.TRIVIAL,
        tags=["approve_plan", "write"],
        setup_commands=[
            ("create_project", {"name": "PlanProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Plan Task", "description": "Has a plan"},
            ),
        ],
        turns=[
            Turn(
                user_message="approve the plan for t-1",
                expected_tools=[
                    ExpectedTool(name="approve_plan", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-approve-plan-lgtm",
        description="Approve plan using natural language",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["approve_plan", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "PlanLGTM"}),
            (
                "create_task",
                {
                    "project_id": "p-1",
                    "title": "Plan Review",
                    "description": "Plan awaiting review",
                },
            ),
        ],
        turns=[
            Turn(
                user_message="the plan for t-1 looks good, go ahead and create the subtasks",
                expected_tools=[
                    ExpectedTool(name="approve_plan", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # reject_plan — TRIVIAL / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="workflow-reject-plan-direct",
        description="Reject a plan with feedback",
        category="task_workflow",
        difficulty=Difficulty.TRIVIAL,
        tags=["reject_plan", "write"],
        setup_commands=[
            ("create_project", {"name": "RejectProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Plan Task", "description": "Has a plan"},
            ),
        ],
        turns=[
            Turn(
                user_message="reject the plan for t-1, it needs to include error handling in phase 2",
                expected_tools=[
                    ExpectedTool(name="reject_plan", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-reject-plan-changes",
        description="Request changes to a plan",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["reject_plan", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "ChangesProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Plan Review", "description": "Plan needs changes"},
            ),
        ],
        turns=[
            Turn(
                user_message="t-1's plan needs work. Add a testing phase and consolidate phases 3 and 4",
                expected_tools=[
                    ExpectedTool(name="reject_plan", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    # -----------------------------------------------------------------------
    # delete_plan — TRIVIAL / MEDIUM
    # -----------------------------------------------------------------------
    TestCase(
        id="workflow-delete-plan-direct",
        description="Delete a plan by task ID",
        category="task_workflow",
        difficulty=Difficulty.TRIVIAL,
        tags=["delete_plan", "write"],
        setup_commands=[
            ("create_project", {"name": "DeleteProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Plan Task", "description": "Has a plan"},
            ),
        ],
        turns=[
            Turn(
                user_message="delete the plan for t-1",
                expected_tools=[
                    ExpectedTool(name="delete_plan", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="workflow-delete-plan-cancel",
        description="Cancel plan execution using natural language",
        category="task_workflow",
        difficulty=Difficulty.MEDIUM,
        tags=["delete_plan", "write", "natural-language"],
        setup_commands=[
            ("create_project", {"name": "CancelProj"}),
            (
                "create_task",
                {"project_id": "p-1", "title": "Plan Task", "description": "Has a plan"},
            ),
        ],
        turns=[
            Turn(
                user_message="actually, nevermind t-1's plan, just scrap it and don't create any subtasks",
                expected_tools=[
                    ExpectedTool(name="delete_plan", args={"task_id": "t-1"}),
                ],
            ),
        ],
    ),
]
