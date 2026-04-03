"""Test cases for note tools (supervisor evaluation).

Covers: list_notes, write_note, delete_note, read_note, append_note,
compare_specs_notes, promote_note.

20 test cases: verified against current supervisor-based architecture.
Notes are per-project knowledge bases managed through the memory tool category.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- list_notes ---
    TestCase(
        id="notes-list-explicit",
        description="List notes for a specific project",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["list_notes"],
        turns=[
            Turn(
                user_message="list notes for project p-1",
                expected_tools=[
                    ExpectedTool(name="list_notes", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-list-active-project",
        description="List notes with active project context",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["list_notes"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show me all notes",
                expected_tools=[ExpectedTool(name="list_notes")],
            ),
        ],
    ),
    TestCase(
        id="notes-list-natural",
        description="List notes with natural phrasing",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["list_notes", "natural-language"],
        active_project="p-2",
        turns=[
            Turn(
                user_message="what notes do we have?",
                expected_tools=[ExpectedTool(name="list_notes")],
            ),
        ],
    ),
    # --- write_note ---
    TestCase(
        id="notes-write-explicit",
        description="Create a new note with title and content",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["write_note"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "write a note titled 'API Design' with content 'REST endpoints for "
                    "user management'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="write_note",
                        args={
                            "title": "API Design",
                            "content": "REST endpoints for user management",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-write-with-project",
        description="Create a note specifying the project explicitly",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["write_note"],
        turns=[
            Turn(
                user_message=(
                    "create a note for project p-2 called 'Architecture' with content "
                    "'Microservices with event sourcing'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="write_note",
                        args={
                            "project_id": "p-2",
                            "title": "Architecture",
                            "content": "Microservices with event sourcing",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-write-natural",
        description="Write a note with natural language",
        category="notes",
        difficulty=Difficulty.MEDIUM,
        tags=["write_note", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="jot down a note called 'Deployment' saying 'use docker compose for staging'",
                expected_tools=[
                    ExpectedTool(
                        name="write_note",
                        args={
                            "title": "Deployment",
                            "content": "use docker compose for staging",
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- read_note ---
    TestCase(
        id="notes-read-explicit",
        description="Read a specific note by title",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["read_note"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="read the 'API Design' note",
                expected_tools=[
                    ExpectedTool(name="read_note", args={"title": "API Design"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-read-with-project",
        description="Read a note from a specific project",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["read_note"],
        turns=[
            Turn(
                user_message="show me the 'Architecture' note from project p-2",
                expected_tools=[
                    ExpectedTool(
                        name="read_note",
                        args={"project_id": "p-2", "title": "Architecture"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-read-natural",
        description="Read a note using natural phrasing",
        category="notes",
        difficulty=Difficulty.MEDIUM,
        tags=["read_note", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what does the 'Deployment' note say?",
                expected_tools=[
                    ExpectedTool(name="read_note", args={"title": "Deployment"}),
                ],
            ),
        ],
    ),
    # --- append_note ---
    TestCase(
        id="notes-append-explicit",
        description="Append content to an existing note",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["append_note"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="append to note 'API Design': add auth endpoint",
                expected_tools=[
                    ExpectedTool(
                        name="append_note",
                        args={"title": "API Design", "content": "add auth endpoint"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-append-natural",
        description="Append to a note using casual phrasing",
        category="notes",
        difficulty=Difficulty.MEDIUM,
        tags=["append_note", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=("add 'remember to handle rate limiting' to the 'API Design' note"),
                expected_tools=[
                    ExpectedTool(
                        name="append_note",
                        args={
                            "title": "API Design",
                            "content": "remember to handle rate limiting",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-append-with-project",
        description="Append to a note in a specific project",
        category="notes",
        difficulty=Difficulty.MEDIUM,
        tags=["append_note"],
        turns=[
            Turn(
                user_message=(
                    "append 'add caching layer' to the 'Architecture' note in project p-2"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="append_note",
                        args={
                            "project_id": "p-2",
                            "title": "Architecture",
                            "content": "add caching layer",
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- delete_note ---
    TestCase(
        id="notes-delete-explicit",
        description="Delete a note by title",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["delete_note"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="delete note 'API Design'",
                expected_tools=[
                    ExpectedTool(name="delete_note", args={"title": "API Design"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-delete-with-project",
        description="Delete a note from a specific project",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["delete_note"],
        turns=[
            Turn(
                user_message="remove the 'Architecture' note from project p-2",
                expected_tools=[
                    ExpectedTool(
                        name="delete_note",
                        args={"project_id": "p-2", "title": "Architecture"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-delete-natural",
        description="Delete a note using natural phrasing",
        category="notes",
        difficulty=Difficulty.MEDIUM,
        tags=["delete_note", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="get rid of the 'Old Roadmap' note",
                expected_tools=[
                    ExpectedTool(name="delete_note", args={"title": "Old Roadmap"}),
                ],
            ),
        ],
    ),
    # --- promote_note ---
    TestCase(
        id="notes-promote-explicit",
        description="Promote a note into the project profile",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["promote_note"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="promote the 'API Design' note to the project profile",
                expected_tools=[
                    ExpectedTool(
                        name="promote_note",
                        args={"project_id": "p-1", "title": "API Design"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-promote-with-project",
        description="Promote a note specifying the project explicitly",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["promote_note"],
        turns=[
            Turn(
                user_message="promote note 'Architecture' in project p-2 to the profile",
                expected_tools=[
                    ExpectedTool(
                        name="promote_note",
                        args={"project_id": "p-2", "title": "Architecture"},
                    ),
                ],
            ),
        ],
    ),
    # --- compare_specs_notes ---
    TestCase(
        id="notes-compare-specs-explicit",
        description="Compare specs and notes for a project",
        category="notes",
        difficulty=Difficulty.EASY,
        tags=["compare_specs_notes"],
        turns=[
            Turn(
                user_message="compare specs with notes for project p-1",
                expected_tools=[
                    ExpectedTool(
                        name="compare_specs_notes",
                        args={"project_id": "p-1"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="notes-compare-specs-natural",
        description="Compare specs and notes with natural phrasing",
        category="notes",
        difficulty=Difficulty.MEDIUM,
        tags=["compare_specs_notes", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what's missing from our notes compared to the specs?",
                expected_tools=[ExpectedTool(name="compare_specs_notes")],
            ),
        ],
    ),
    TestCase(
        id="notes-compare-specs-custom-path",
        description="Compare specs and notes with a custom specs directory",
        category="notes",
        difficulty=Difficulty.MEDIUM,
        tags=["compare_specs_notes"],
        turns=[
            Turn(
                user_message=("compare specs in docs/specifications with notes for project p-1"),
                expected_tools=[
                    ExpectedTool(name="compare_specs_notes"),
                ],
            ),
        ],
    ),
]
