"""Test cases for workspace management tools (supervisor evaluation).

Covers: add_workspace, list_workspaces, find_merge_conflict_workspaces,
sync_workspaces, remove_workspace, release_workspace.

19 test cases: verified against current supervisor-based architecture.
Workspaces are git working directories assigned to agents for task execution.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- add_workspace ---
    TestCase(
        id="ws-add-link-explicit",
        description="Add a workspace by linking an existing directory to a project",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["add_workspace", "link"],
        turns=[
            Turn(
                user_message="add a workspace for project p-1 at /home/dev/myapp",
                expected_tools=[
                    ExpectedTool(
                        name="add_workspace",
                        args={"project_id": "p-1", "source": "link", "path": "/home/dev/myapp"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-add-link-natural",
        description="Link a directory to a project using natural phrasing",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["add_workspace", "link", "natural-language"],
        turns=[
            Turn(
                user_message="link /path/to/repo to project p-1",
                expected_tools=[
                    ExpectedTool(
                        name="add_workspace",
                        args={"project_id": "p-1", "source": "link", "path": "/path/to/repo"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-add-link-connect-phrasing",
        description="Connect an existing directory as workspace using 'connect' verb",
        category="workspaces",
        difficulty=Difficulty.MEDIUM,
        tags=["add_workspace", "link", "natural-language"],
        turns=[
            Turn(
                user_message="connect /srv/projects/webapp to the webapp project",
                expected_tools=[
                    ExpectedTool(name="add_workspace"),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-add-clone",
        description="Add a workspace by cloning the project repo",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["add_workspace", "clone"],
        turns=[
            Turn(
                user_message="clone a new workspace for project p-2",
                expected_tools=[
                    ExpectedTool(
                        name="add_workspace",
                        args={"project_id": "p-2", "source": "clone"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-add-with-name",
        description="Add a named workspace for a project",
        category="workspaces",
        difficulty=Difficulty.MEDIUM,
        tags=["add_workspace", "link"],
        turns=[
            Turn(
                user_message=(
                    "add a workspace called 'staging' for project p-1 pointing to /opt/staging/app"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="add_workspace",
                        args={
                            "project_id": "p-1",
                            "source": "link",
                            "path": "/opt/staging/app",
                            "name": "staging",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-add-use-phrasing",
        description="User says 'use this directory' implying link source type",
        category="workspaces",
        difficulty=Difficulty.MEDIUM,
        tags=["add_workspace", "link", "natural-language"],
        active_project="p-3",
        turns=[
            Turn(
                user_message="use /home/dev/api-server as a workspace",
                expected_tools=[
                    ExpectedTool(
                        name="add_workspace",
                        args={"source": "link", "path": "/home/dev/api-server"},
                    ),
                ],
            ),
        ],
    ),
    # --- list_workspaces ---
    TestCase(
        id="ws-list-all",
        description="List all workspaces across all projects",
        category="workspaces",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_workspaces"],
        turns=[
            Turn(
                user_message="list all workspaces",
                expected_tools=[ExpectedTool(name="list_workspaces")],
            ),
        ],
    ),
    TestCase(
        id="ws-list-status",
        description="Show workspace status using alternative phrasing",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["list_workspaces", "natural-language"],
        turns=[
            Turn(
                user_message="show workspace status",
                expected_tools=[ExpectedTool(name="list_workspaces")],
            ),
        ],
    ),
    TestCase(
        id="ws-list-by-project",
        description="List workspaces filtered to a specific project",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["list_workspaces"],
        turns=[
            Turn(
                user_message="show workspaces for project p-1",
                expected_tools=[
                    ExpectedTool(name="list_workspaces", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-list-which-locked",
        description="User asks which workspaces are locked (maps to list)",
        category="workspaces",
        difficulty=Difficulty.MEDIUM,
        tags=["list_workspaces", "natural-language"],
        turns=[
            Turn(
                user_message="which workspaces are currently locked?",
                expected_tools=[ExpectedTool(name="list_workspaces")],
            ),
        ],
    ),
    # --- find_merge_conflict_workspaces ---
    TestCase(
        id="ws-find-merge-conflicts",
        description="Find workspaces with merge conflicts",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["find_merge_conflict_workspaces"],
        turns=[
            Turn(
                user_message="are there any workspaces with merge conflicts?",
                expected_tools=[ExpectedTool(name="find_merge_conflict_workspaces")],
            ),
        ],
    ),
    TestCase(
        id="ws-find-merge-conflicts-variant",
        description="Check for conflicting workspaces using natural phrasing",
        category="workspaces",
        difficulty=Difficulty.MEDIUM,
        tags=["find_merge_conflict_workspaces"],
        turns=[
            Turn(
                user_message="which workspaces have conflicts right now?",
                expected_tools=[ExpectedTool(name="find_merge_conflict_workspaces")],
            ),
        ],
    ),
    # --- queue_sync_workspaces ---
    TestCase(
        id="ws-sync-all",
        description="Queue sync task for all workspaces",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["queue_sync_workspaces"],
        turns=[
            Turn(
                user_message="sync all workspaces",
                expected_tools=[ExpectedTool(name="queue_sync_workspaces")],
            ),
        ],
    ),
    TestCase(
        id="ws-sync-project",
        description="Queue sync task for a specific project's workspaces",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["queue_sync_workspaces"],
        turns=[
            Turn(
                user_message="sync the workspaces for project p-1",
                expected_tools=[
                    ExpectedTool(name="queue_sync_workspaces", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    # --- remove_workspace ---
    TestCase(
        id="ws-remove-explicit",
        description="Remove a workspace by ID",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["remove_workspace"],
        turns=[
            Turn(
                user_message="remove workspace ws-3",
                expected_tools=[
                    ExpectedTool(name="remove_workspace", args={"workspace_id": "ws-3"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-remove-delete-phrasing",
        description="Delete a workspace using the 'delete' verb",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["remove_workspace", "natural-language"],
        turns=[
            Turn(
                user_message="delete workspace ws-5 from project p-1",
                expected_tools=[
                    ExpectedTool(
                        name="remove_workspace",
                        args={"workspace_id": "ws-5", "project_id": "p-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-remove-natural",
        description="Remove a workspace using natural phrasing",
        category="workspaces",
        difficulty=Difficulty.MEDIUM,
        tags=["remove_workspace", "natural-language"],
        active_project="p-2",
        turns=[
            Turn(
                user_message="I don't need workspace ws-4 anymore, please get rid of it",
                expected_tools=[
                    ExpectedTool(name="remove_workspace", args={"workspace_id": "ws-4"}),
                ],
            ),
        ],
    ),
    # --- release_workspace ---
    TestCase(
        id="ws-release-explicit",
        description="Release a workspace lock by ID",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["release_workspace"],
        turns=[
            Turn(
                user_message="release workspace ws-1",
                expected_tools=[
                    ExpectedTool(name="release_workspace", args={"workspace_id": "ws-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="ws-release-unlock-phrasing",
        description="Unlock a workspace using the 'unlock' verb",
        category="workspaces",
        difficulty=Difficulty.EASY,
        tags=["release_workspace", "natural-language"],
        turns=[
            Turn(
                user_message="unlock workspace ws-2",
                expected_tools=[
                    ExpectedTool(name="release_workspace", args={"workspace_id": "ws-2"}),
                ],
            ),
        ],
    ),
]
