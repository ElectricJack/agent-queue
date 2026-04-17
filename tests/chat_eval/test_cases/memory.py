"""Test cases for memory tools (supervisor evaluation).

Covers: memory_store, memory_recall, memory_delete.

These are the three agent-facing memory tools. All other memory operations
(search, stats, reindex, compact, health, etc.) are internal/admin only.

Updated: simplified memory interface — only memory_store, memory_recall,
memory_delete are agent-facing tools.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- memory_recall ---
    TestCase(
        id="memory-recall-basic",
        description="Recall project memory with explicit project and query",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["memory_recall"],
        turns=[
            Turn(
                user_message="search memory in test-project for 'authentication'",
                expected_tools=[
                    ExpectedTool(
                        name="memory_recall",
                        args={"project_id": "test-project", "query": "authentication"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-recall-natural",
        description="Recall memory with natural phrasing",
        category="memory",
        difficulty=Difficulty.MEDIUM,
        tags=["memory_recall"],
        turns=[
            Turn(
                user_message="what do we know about the database migration in project backend?",
                expected_tools=[
                    ExpectedTool(
                        name="memory_recall",
                        args={"project_id": "backend"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-recall-active-project",
        description="Recall memory using active project context",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["memory_recall"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="search memory for 'API rate limiting'",
                expected_tools=[
                    ExpectedTool(
                        name="memory_recall",
                        args={"project_id": "my-app", "query": "API rate limiting"},
                    ),
                ],
            ),
        ],
    ),
    # --- memory_store ---
    TestCase(
        id="memory-store-basic",
        description="Store an insight in project memory",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["memory_store"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="remember that the test framework is pytest with asyncio mode",
                expected_tools=[
                    ExpectedTool(
                        name="memory_store",
                        args={"project_id": "my-app"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-store-natural",
        description="Store knowledge naturally",
        category="memory",
        difficulty=Difficulty.MEDIUM,
        tags=["memory_store"],
        active_project="backend",
        turns=[
            Turn(
                user_message="save to memory: database migrations must run before deploying",
                expected_tools=[
                    ExpectedTool(
                        name="memory_store",
                        args={"project_id": "backend"},
                    ),
                ],
            ),
        ],
    ),
    # --- memory_delete ---
    TestCase(
        id="memory-delete-basic",
        description="Delete a memory entry by hash",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["memory_delete"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="delete memory entry abc123def",
                expected_tools=[
                    ExpectedTool(
                        name="memory_delete",
                        args={"chunk_hash": "abc123def"},
                    ),
                ],
            ),
        ],
    ),
    # --- multi-turn memory usage ---
    TestCase(
        id="memory-recall-then-store",
        description="Recall memory then store new insight in a multi-turn conversation",
        category="memory",
        difficulty=Difficulty.MEDIUM,
        tags=["memory_recall", "memory_store"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="search memory for 'deployment pipeline'",
                expected_tools=[
                    ExpectedTool(
                        name="memory_recall",
                        args={"project_id": "my-app", "query": "deployment pipeline"},
                    ),
                ],
            ),
            Turn(
                user_message="save this insight: deployment requires running migrations first",
                expected_tools=[
                    ExpectedTool(
                        name="memory_store",
                        args={"project_id": "my-app"},
                    ),
                ],
            ),
        ],
    ),
]
