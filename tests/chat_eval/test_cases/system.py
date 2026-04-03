"""Test cases for system tools (supervisor evaluation).

Covers: get_status, get_recent_events, get_token_usage, read_file, run_command,
search_files, restart_daemon, orchestrator_control, list_prompts, read_prompt,
render_prompt.

28 test cases: verified against current supervisor-based architecture.
System tools are loaded on-demand via the 'system' tool category.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- get_status ---
    TestCase(
        id="system-status-simple",
        description="Show system status overview",
        category="system",
        difficulty=Difficulty.TRIVIAL,
        tags=["get_status"],
        turns=[
            Turn(
                user_message="show system status",
                expected_tools=[ExpectedTool(name="get_status")],
            ),
        ],
    ),
    TestCase(
        id="system-status-natural",
        description="Ask for system status with natural phrasing",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["get_status", "natural-language"],
        turns=[
            Turn(
                user_message="how's everything looking?",
                expected_tools=[ExpectedTool(name="get_status")],
            ),
        ],
    ),
    # --- get_recent_events ---
    TestCase(
        id="system-events-simple",
        description="Get recent system events",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["get_recent_events"],
        turns=[
            Turn(
                user_message="what happened recently?",
                expected_tools=[ExpectedTool(name="get_recent_events")],
            ),
        ],
    ),
    TestCase(
        id="system-events-with-limit",
        description="Get a specific number of recent events",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["get_recent_events"],
        turns=[
            Turn(
                user_message="show the last 20 events",
                expected_tools=[
                    ExpectedTool(name="get_recent_events", args={"limit": 20}),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-events-natural",
        description="Ask about recent activity with casual phrasing",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["get_recent_events", "natural-language"],
        turns=[
            Turn(
                user_message="any new events I should know about?",
                expected_tools=[ExpectedTool(name="get_recent_events")],
            ),
        ],
    ),
    # --- get_token_usage ---
    TestCase(
        id="system-token-usage-global",
        description="Show token usage across all projects",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["get_token_usage"],
        turns=[
            Turn(
                user_message="show token usage",
                expected_tools=[ExpectedTool(name="get_token_usage")],
            ),
        ],
    ),
    TestCase(
        id="system-token-usage-by-project",
        description="Show token usage for a specific project",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["get_token_usage"],
        turns=[
            Turn(
                user_message="how many tokens has project p-1 used?",
                expected_tools=[
                    ExpectedTool(name="get_token_usage", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    # --- read_file ---
    TestCase(
        id="system-read-file",
        description="Read a specific file from a workspace",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["read_file"],
        turns=[
            Turn(
                user_message="read file src/main.py",
                expected_tools=[
                    ExpectedTool(name="read_file", args={"path": "src/main.py"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-read-file-absolute",
        description="Read a file by absolute path",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["read_file"],
        turns=[
            Turn(
                user_message="show me the contents of /home/dev/project/config.yaml",
                expected_tools=[
                    ExpectedTool(
                        name="read_file",
                        args={"path": "/home/dev/project/config.yaml"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-read-file-with-limit",
        description="Read a file with a line limit",
        category="system",
        difficulty=Difficulty.MEDIUM,
        tags=["read_file"],
        turns=[
            Turn(
                user_message="show the first 50 lines of src/orchestrator.py",
                expected_tools=[
                    ExpectedTool(
                        name="read_file",
                        args={"path": "src/orchestrator.py", "max_lines": 50},
                    ),
                ],
            ),
        ],
    ),
    # --- run_command ---
    TestCase(
        id="system-run-command-tests",
        description="Run a test command in a specific directory",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["run_command"],
        turns=[
            Turn(
                user_message="run 'pytest tests/' in /home/dev",
                expected_tools=[
                    ExpectedTool(
                        name="run_command",
                        args={"command": "pytest tests/", "working_dir": "/home/dev"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-run-command-natural",
        description="Run a command using natural language",
        category="system",
        difficulty=Difficulty.MEDIUM,
        tags=["run_command", "natural-language"],
        turns=[
            Turn(
                user_message="execute 'ls -la' in /var/log",
                expected_tools=[
                    ExpectedTool(
                        name="run_command",
                        args={"command": "ls -la", "working_dir": "/var/log"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-run-command-with-timeout",
        description="Run a command with a custom timeout",
        category="system",
        difficulty=Difficulty.MEDIUM,
        tags=["run_command"],
        turns=[
            Turn(
                user_message=("run 'npm run build' in /home/dev/frontend with a 60 second timeout"),
                expected_tools=[
                    ExpectedTool(
                        name="run_command",
                        args={
                            "command": "npm run build",
                            "working_dir": "/home/dev/frontend",
                            "timeout": 60,
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- search_files ---
    TestCase(
        id="system-search-grep",
        description="Search for a pattern in file contents",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["search_files"],
        turns=[
            Turn(
                user_message="search for 'TODO' in src/",
                expected_tools=[
                    ExpectedTool(name="search_files"),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-search-find-mode",
        description="Search for files by name",
        category="system",
        difficulty=Difficulty.MEDIUM,
        tags=["search_files"],
        turns=[
            Turn(
                user_message="find all Python files in /home/dev/project",
                expected_tools=[
                    ExpectedTool(
                        name="search_files",
                        args={
                            "pattern": "*.py",
                            "path": "/home/dev/project",
                            "mode": "find",
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- restart_daemon ---
    TestCase(
        id="system-restart-daemon",
        description="Restart the daemon process",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["restart_daemon"],
        turns=[
            Turn(
                user_message="restart the daemon",
                expected_tools=[ExpectedTool(name="restart_daemon")],
            ),
        ],
    ),
    TestCase(
        id="system-restart-daemon-natural",
        description="Restart the daemon using natural phrasing",
        category="system",
        difficulty=Difficulty.MEDIUM,
        tags=["restart_daemon", "natural-language"],
        turns=[
            Turn(
                user_message="reboot the system",
                expected_tools=[ExpectedTool(name="restart_daemon")],
            ),
        ],
    ),
    # --- orchestrator_control ---
    TestCase(
        id="system-orchestrator-pause",
        description="Pause the orchestrator",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["orchestrator_control"],
        turns=[
            Turn(
                user_message="pause the orchestrator",
                expected_tools=[
                    ExpectedTool(
                        name="orchestrator_control",
                        args={"action": "pause"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-orchestrator-resume",
        description="Resume the orchestrator",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["orchestrator_control"],
        turns=[
            Turn(
                user_message="resume the orchestrator",
                expected_tools=[
                    ExpectedTool(
                        name="orchestrator_control",
                        args={"action": "resume"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-orchestrator-status",
        description="Check orchestrator status",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["orchestrator_control"],
        turns=[
            Turn(
                user_message="is the orchestrator running?",
                expected_tools=[
                    ExpectedTool(
                        name="orchestrator_control",
                        args={"action": "status"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-orchestrator-stop-natural",
        description="Pause orchestrator using 'stop' phrasing",
        category="system",
        difficulty=Difficulty.MEDIUM,
        tags=["orchestrator_control", "natural-language"],
        turns=[
            Turn(
                user_message="stop assigning new tasks",
                expected_tools=[
                    ExpectedTool(
                        name="orchestrator_control",
                        args={"action": "pause"},
                    ),
                ],
            ),
        ],
    ),
    # --- list_prompts ---
    TestCase(
        id="system-list-prompts",
        description="List prompt templates for a project",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["list_prompts"],
        turns=[
            Turn(
                user_message="list prompts for project p-1",
                expected_tools=[
                    ExpectedTool(name="list_prompts", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-list-prompts-active",
        description="List prompts with active project context",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["list_prompts"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show all prompt templates",
                expected_tools=[ExpectedTool(name="list_prompts")],
            ),
        ],
    ),
    TestCase(
        id="system-list-prompts-by-category",
        description="List prompts filtered by category",
        category="system",
        difficulty=Difficulty.MEDIUM,
        tags=["list_prompts"],
        turns=[
            Turn(
                user_message="show the task prompts for project p-1",
                expected_tools=[
                    ExpectedTool(
                        name="list_prompts",
                        args={"project_id": "p-1", "category": "task"},
                    ),
                ],
            ),
        ],
    ),
    # --- read_prompt ---
    TestCase(
        id="system-read-prompt",
        description="Read a specific prompt template",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["read_prompt"],
        turns=[
            Turn(
                user_message="show me the 'plan-generation' prompt for project p-1",
                expected_tools=[
                    ExpectedTool(
                        name="read_prompt",
                        args={"project_id": "p-1", "name": "plan-generation"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-read-prompt-active",
        description="Read a prompt template with active project",
        category="system",
        difficulty=Difficulty.EASY,
        tags=["read_prompt"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="read the 'task-instructions' prompt",
                expected_tools=[
                    ExpectedTool(
                        name="read_prompt",
                        args={"name": "task-instructions"},
                    ),
                ],
            ),
        ],
    ),
    # --- render_prompt ---
    TestCase(
        id="system-render-prompt",
        description="Render a prompt template with variables",
        category="system",
        difficulty=Difficulty.MEDIUM,
        tags=["render_prompt"],
        turns=[
            Turn(
                user_message=(
                    "render the 'task-instructions' prompt for project p-1 with "
                    "task_title='Fix login bug'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="render_prompt",
                        args={
                            "project_id": "p-1",
                            "name": "task-instructions",
                            "variables": {"task_title": "Fix login bug"},
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="system-render-prompt-natural",
        description="Render a prompt using natural language",
        category="system",
        difficulty=Difficulty.HARD,
        tags=["render_prompt", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "fill in the 'plan-generation' prompt where project_name is "
                    "'API Gateway' and scope is 'authentication'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="render_prompt",
                        args={
                            "name": "plan-generation",
                            "variables": {
                                "project_name": "API Gateway",
                                "scope": "authentication",
                            },
                        },
                    ),
                ],
            ),
        ],
    ),
]
