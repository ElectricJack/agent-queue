"""Tests for L0 + L1 tier injection — Roadmap 3.3.7.

Verifies that L0 Identity (~50 tokens) and L1 Critical Facts (~200 tokens)
reach the agent at the correct tier positions.

Test cases from docs/specs/design/roadmap.md §3.3.7:
  (a) Task context includes Role from profile (L0)
  (b) Task context includes project + agent-type facts (L1)
  (c) Combined L0+L1 ≈ 250 tokens baseline
  (d) L0 absent if no profile (graceful degradation)
  (e) L1 absent if no facts.md (no error)
  (f) L0+L1 in system prompt section (not user message)
  (g) Agent with profile but no project still gets L0 + agent-type L1

**Where the tiers live now.**  The orchestrator used to compute ``l0_role``
and ``l1_facts`` onto a :class:`~src.models.TaskContext` and hand it to a
runtime adapter.  That path was deleted with the runtime subsystem: every
agent runs as a session, the launch carries only the bootstrap prompt
(``src/sessions/spec.py::BOOTSTRAP_PROMPT``), and the agent fetches its full
startup document with ``aq prime``.  The tiers are therefore sections of the
prime document (``src/prime/sections.py``, design §5.2):

* L0 — ``build_role_section``: the ``## Role`` (+ ``## Rules``) body of
  ``vault/agent-types/<profile_id>/profile.md``.  ``AgentProfile.system_prompt_suffix``
  is no longer read on this path.
* L1 — ``build_l1_facts_section``: an always-present slot that renders empty
  while ``memory.enabled`` is False (docs/specs/design/feature-pauses.md).

The (a)/(d)/(e)/(g) cases below drive the real dispatch path with the
``fake`` session provider (``tests/session_dispatch_helpers.py``) and assert
on the prime document rendered for the launched task, so a regression in
either dispatch or prime wiring fails them.  Pure renderer coverage lives in
``tests/test_prime_renderer.py``.

**Cases that no longer have an implementation to test.**  Nothing in
``src/`` calls ``MemoryService.load_l1_facts`` any more, so the following
former tests are removed rather than ported — there is no L1-from-memory
behaviour on the session path to assert:

* ``TestL1FactsFromMemory::test_l1_facts_populated_from_memory_service``
* ``TestL1FactsFromMemory::test_l1_facts_called_with_project_and_agent_type``
* ``TestL0L1ProfileWithoutProjectFacts::test_memory_service_called_with_agent_type``
* ``TestL0L1ProfileWithoutProjectFacts::test_agent_type_none_when_no_profile``

What *does* exist — the L1 slot rendering empty while memory is paused — is
pinned by ``tests/test_prime_renderer.py::TestMemoryPausedSlots``
(``test_l1_and_l2_render_empty_while_memory_paused`` and
``test_l1_and_l2_slots_still_present_as_section_vars``) and, end to end, by
``TestL1GracefulDegradation`` below.  Re-wiring L1 content is a renderer
change in ``build_l1_facts_section`` (see its docstring); the memory-service
tests belong back here when that lands.
"""

from unittest.mock import AsyncMock

from src.models import Task, TaskContext, TaskStatus
from tests.session_dispatch_helpers import (
    create_session_profile,
    create_session_project,
    drain_running_tasks,
    fake_provider,
    prime_bodies,
    render_prime,
    write_vault_profile,
)

# -- Realistic L0/L1 content for token budget tests ----------------------

L0_ROLE_REALISTIC = (
    "You are a senior software engineering agent. You write, modify, and debug "
    "production-quality Python code within an async project workspace. Follow "
    "the project's conventions, run tests before committing."
)  # ~50 tokens at 4 chars/token ≈ 200 chars

L1_FACTS_REALISTIC = (
    "## Critical Facts\n"
    "- tech_stack: Python 3.12, asyncio, SQLAlchemy Core, FastAPI, discord.py\n"
    "- test_framework: pytest with pytest-asyncio in auto mode, ruff for linting\n"
    "- linter: ruff format and lint (line-length 100, target py312, pre-commit hooks)\n"
    "- default_branch: main (protected, requires PR review before merge)\n"
    "- deploy_target: staging environment via Docker Compose with PostgreSQL\n"
    "- database: SQLite with aiosqlite for dev, PostgreSQL with asyncpg for prod\n"
    "- ci_cd: GitHub Actions for tests, linting, and documentation deployment\n"
    "- api_style: RESTful JSON endpoints with FastAPI OpenAPI documentation\n"
    "- auth: JWT bearer tokens with refresh token rotation\n"
    "- logging: structlog with structured JSON and correlation IDs"
)  # ~200 tokens at 4 chars/token ≈ 800 chars


# -- Test helpers --------------------------------------------------------


def _build_prompt_from(task: TaskContext) -> str:
    """Assemble a prompt from *task* the way the dispatcher does.

    This mirrors the tier wiring that used to live in
    ``ClaudeSDKRuntime._build_prompt``.  That runtime was deleted in the
    tmux-harness migration, but the thing under test here was never the
    runtime — it is :class:`~src.prompt_builder.PromptBuilder` and the tier
    ordering contract (L0 -> override -> L1 -> L2 -> description).  Driving
    the builder directly tests that contract without routing through a
    dispatch path, and keeps the tests meaningful for session-run agents.
    """
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder()
    if task.l0_role:
        builder.set_l0_role(task.l0_role)
    if task.project_override_role:
        builder.set_override_content(task.project_override_role)
    if task.l1_facts:
        builder.set_l1_facts(task.l1_facts)
    if task.l1_guidance:
        builder.set_l1_guidance(task.l1_guidance)
    if task.l2_context:
        builder.set_l2_context(task.l2_context)
    if task.description:
        builder.add_context("description", task.description)
    if task.acceptance_criteria:
        criteria = "\n".join(f"- {c}" for c in task.acceptance_criteria)
        builder.add_context("acceptance_criteria", f"## Acceptance Criteria\n{criteria}")
    if task.test_commands:
        cmds = "\n".join(f"- `{c}`" for c in task.test_commands)
        builder.add_context("test_commands", f"## Test Commands\n{cmds}")
    # ``build()`` returns (system_prompt, tools); task execution wants the
    # flat string, which is what the deleted adapter returned.
    return builder.build_task_prompt()


async def _dispatch(orch, *, task_id: str = "t-1", profile_id: str | None = None) -> Task:
    """Create a READY task, run one cycle, and wait for its launch to settle."""
    await orch.db.create_task(
        Task(
            id=task_id,
            project_id="p-1",
            title="Tier injection",
            description="Do something",
            status=TaskStatus.READY,
            profile_id=profile_id,
        )
    )
    await orch.run_one_cycle()
    await drain_running_tasks(orch)
    return await orch.db.get_task(task_id)


async def _assert_launched(orch, task_id: str = "t-1") -> None:
    """The task actually left READY as a session — prime is rendered *for a launch*."""
    task = await orch.db.get_task(task_id)
    assert task.status == TaskStatus.IN_PROGRESS, task.status
    session = await orch.db.get_session_for_task(task_id)
    assert session is not None and session.state == "running"
    assert fake_provider(orch).starts, "provider.start() was never called"


# ======================================================================
# (a) Every task context includes Role from profile (L0, ~50 tokens)
# ======================================================================


class TestL0RoleFromProfile:
    """(a) The launched task's prime carries the ``## Role`` of its profile."""

    async def test_l0_role_from_task_profile_vault_file(self, session_orch):
        """``vault/agent-types/<profile>/profile.md`` ``## Role`` is prime section 1."""
        orch = session_orch
        await create_session_project(orch)
        await create_session_profile(orch, "coding")
        write_vault_profile(
            orch.config,
            "coding",
            "## Role\nYou are a senior backend developer.\n\n## Config\nharness: claude\n",
        )

        await _dispatch(orch, profile_id="coding")
        await _assert_launched(orch)

        bodies = prime_bodies(await render_prime(orch, "t-1"))
        assert bodies["role"] == "You are a senior backend developer."

    async def test_l0_role_stripped_of_whitespace(self, session_orch):
        """Leading/trailing whitespace around the Role body is stripped."""
        orch = session_orch
        await create_session_project(orch)
        await create_session_profile(orch, "qa")
        write_vault_profile(orch.config, "qa", "## Role\n  \n  You are a QA specialist.  \n  \n")

        await _dispatch(orch, profile_id="qa")
        await _assert_launched(orch)

        bodies = prime_bodies(await render_prime(orch, "t-1"))
        assert bodies["role"] == "You are a QA specialist."

    async def test_l0_role_from_project_default_profile(self, session_orch):
        """A task without its own profile_id still gets L0 from the project default."""
        orch = session_orch
        await create_session_project(orch, default_profile_id="coding")
        write_vault_profile(orch.config, "coding", "## Role\nYou are a full-stack developer.\n")

        await _dispatch(orch)
        await _assert_launched(orch)

        # Dispatch resolved the project default onto the session…
        session = await orch.db.get_session_for_task("t-1")
        assert session.profile_id == "coding"
        # …so the agent that session runs must be told who it is.
        bodies = prime_bodies(await render_prime(orch, "t-1"))
        assert bodies["role"] == "You are a full-stack developer."


# ======================================================================
# (c) Combined L0+L1 ≈ 250 tokens baseline (verify within tolerance)
# ======================================================================


class TestL0L1TokenBudget:
    """(c) Combined L0+L1 is approximately 250 tokens baseline."""

    # Token estimation: 1 token ≈ 4 characters (same as prompt_builder.py)
    CHARS_PER_TOKEN = 4

    def test_l0_role_within_50_token_budget(self):
        """L0 role text is approximately 50 tokens."""
        tokens = len(L0_ROLE_REALISTIC) / self.CHARS_PER_TOKEN
        assert 30 <= tokens <= 80, (
            f"L0 should be ~50 tokens, got {tokens:.0f} ({len(L0_ROLE_REALISTIC)} chars)"
        )

    def test_l1_facts_within_200_token_budget(self):
        """L1 facts text is approximately 200 tokens."""
        tokens = len(L1_FACTS_REALISTIC) / self.CHARS_PER_TOKEN
        assert 120 <= tokens <= 280, (
            f"L1 should be ~200 tokens, got {tokens:.0f} ({len(L1_FACTS_REALISTIC)} chars)"
        )

    def test_combined_l0_l1_approximately_250_tokens(self):
        """Combined L0+L1 is within tolerance of 250-token baseline."""
        l0_tokens = len(L0_ROLE_REALISTIC) / self.CHARS_PER_TOKEN
        l1_tokens = len(L1_FACTS_REALISTIC) / self.CHARS_PER_TOKEN
        combined = l0_tokens + l1_tokens

        # 250 tokens ±50% tolerance (150–375)
        assert 150 <= combined <= 375, (
            f"Combined L0+L1 should be ~250 tokens, got {combined:.0f} "
            f"(L0={l0_tokens:.0f}, L1={l1_tokens:.0f})"
        )

    def test_combined_prompt_stays_within_budget(self):
        """L0+L1 injected through PromptBuilder stays within ~250 tokens."""
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_l0_role(L0_ROLE_REALISTIC)
        builder.set_l1_facts(L1_FACTS_REALISTIC)
        prompt = builder.build_task_prompt()

        # The prompt includes the L0 and L1 content plus "---" separators.
        # Verify the overhead from separators is minimal.
        prompt_tokens = len(prompt) / self.CHARS_PER_TOKEN
        raw_tokens = (len(L0_ROLE_REALISTIC) + len(L1_FACTS_REALISTIC)) / self.CHARS_PER_TOKEN

        # Separator overhead should be < 10 tokens
        overhead = prompt_tokens - raw_tokens
        assert overhead < 10, f"Separator overhead is {overhead:.0f} tokens, expected < 10"


# ======================================================================
# (d) L0 absent if agent has no profile.md (graceful degradation)
# ======================================================================


class TestL0GracefulDegradation:
    """(d) L0 is absent if the profile has no ``profile.md`` — and nothing breaks.

    "No profile at all" is not a degradation case on the session path: a
    task without a routable profile has no harness and is not launched
    (``tests/test_agent_profiles.py::TestProfileEnforcement::
    test_dispatch_with_no_profile_anywhere_launches_no_session``).  What
    degrades gracefully is a routable profile with nothing to say.
    """

    async def test_l0_empty_when_profile_has_no_vault_file(self, session_orch):
        """Profile row exists, no ``profile.md`` in the vault → empty role section."""
        orch = session_orch
        await create_session_project(orch)
        await create_session_profile(orch, "bare")

        await _dispatch(orch, profile_id="bare")
        await _assert_launched(orch)

        doc = await render_prime(orch, "t-1")
        assert prime_bodies(doc)["role"] == ""
        assert "## Role" not in doc.to_markdown()

    async def test_l0_empty_when_profile_has_no_role_heading(self, session_orch):
        """``profile.md`` exists but has only machine-only headings → empty role."""
        orch = session_orch
        await create_session_project(orch)
        await create_session_profile(orch, "bare")
        write_vault_profile(orch.config, "bare", "## Config\nharness: claude\n\n## Tools\n- Read\n")

        await _dispatch(orch, profile_id="bare")
        await _assert_launched(orch)

        assert prime_bodies(await render_prime(orch, "t-1"))["role"] == ""

    async def test_task_launches_without_l0(self, session_orch):
        """The session starts and the task is IN_PROGRESS even with no L0 role."""
        orch = session_orch
        await create_session_project(orch)
        await create_session_profile(orch, "bare")

        await _dispatch(orch, profile_id="bare")

        await _assert_launched(orch)
        # The rest of the document is intact.
        bodies = prime_bodies(await render_prime(orch, "t-1"))
        assert "Do something" in bodies["task"]
        assert bodies["completion_protocol"]


# ======================================================================
# (e) L1 absent if no facts.md exists for the scope (no error)
# ======================================================================


class TestL1GracefulDegradation:
    """(e) The L1 slot is empty when there is nothing to fill it, without error."""

    async def test_l1_empty_when_no_memory_service(self, session_orch):
        """No memory plugin, memory paused → empty L1, task launched."""
        orch = session_orch
        assert orch.plugin_registry.get_service("memory") is None
        assert orch.config.memory.enabled is False
        await create_session_project(orch)

        await _dispatch(orch)
        await _assert_launched(orch)

        doc = await render_prime(orch, "t-1")
        assert prime_bodies(doc)["l1_facts"] == ""
        assert "## Facts" not in doc.to_markdown()

    async def test_l1_graceful_when_memory_service_would_raise(self, session_orch):
        """A broken memory service cannot break the launch or the prime render.

        The session path does not consult the plugin service for L1 (prime's
        slot is config-gated), so the failure mode the old test guarded —
        ``load_l1_facts`` raising mid-dispatch — cannot reach the agent.
        """
        orch = session_orch
        mock_mem = AsyncMock()
        mock_mem.load_l1_facts = AsyncMock(side_effect=RuntimeError("memsearch unavailable"))
        orch.plugin_registry.register_plugin_service("aq-memory", "memory", mock_mem)
        await create_session_project(orch)

        await _dispatch(orch)
        await _assert_launched(orch)

        assert prime_bodies(await render_prime(orch, "t-1"))["l1_facts"] == ""
        mock_mem.load_l1_facts.assert_not_called()

    async def test_l1_slot_present_but_empty_in_section_vars(self, session_orch):
        """The slot survives as a template variable so a memory comeback is a renderer change."""
        orch = session_orch
        await create_session_project(orch)

        await _dispatch(orch)

        variables = (await render_prime(orch, "t-1")).section_vars()
        assert "l1_facts" in variables and variables["l1_facts"] == ""


# ======================================================================
# (f) L0+L1 content appears in system prompt section (not user message)
# ======================================================================


class TestL0L1InSystemPrompt:
    """(f) L0+L1 content appears in the system prompt section (not user message)."""

    def test_l0_l1_present_in_adapter_system_prompt(self):
        """L0 and L1 appear in the assembled prompt."""
        task = TaskContext(
            description="## Task\nImplement the feature.",
            l0_role=L0_ROLE_REALISTIC,
            l1_facts=L1_FACTS_REALISTIC,
        )
        prompt = _build_prompt_from(task)

        assert L0_ROLE_REALISTIC in prompt
        assert "Critical Facts" in prompt
        assert "tech_stack: Python 3.12" in prompt

    def test_l0_before_l1_before_description_in_prompt(self):
        """System prompt ordering: L0 → L1 → description."""
        task = TaskContext(
            description="## Task\nImplement the feature.",
            l0_role="You are a QA agent.",
            l1_facts="## Critical Facts\n- lang: Python",
        )
        prompt = _build_prompt_from(task)

        role_pos = prompt.index("You are a QA agent.")
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Implement the feature.")
        assert role_pos < facts_pos < task_pos

    def test_l0_l1_assembled_via_prompt_builder(self):
        """L0+L1 are injected through PromptBuilder's layered assembly."""
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_l0_role("You are a coding agent.")
        builder.set_l1_facts("## Critical Facts\n- stack: Python")
        builder.add_context("description", "## Task\nFix the bug.")

        system_prompt, _tools = builder.build()

        # L0 and L1 are in the system prompt string
        assert "You are a coding agent." in system_prompt
        assert "Critical Facts" in system_prompt
        assert "Fix the bug." in system_prompt

        # Correct ordering in system prompt
        role_pos = system_prompt.index("You are a coding agent.")
        facts_pos = system_prompt.index("Critical Facts")
        task_pos = system_prompt.index("Fix the bug.")
        assert role_pos < facts_pos < task_pos

    def test_l0_l1_with_all_task_context_extras(self):
        """L0+L1 coexist with acceptance criteria and test commands in prompt."""
        task = TaskContext(
            description="## Task\nBuild the API.",
            l0_role="You are an API developer.",
            l1_facts="## Critical Facts\n- framework: FastAPI",
            acceptance_criteria=["Endpoint returns 200", "Tests pass"],
            test_commands=["pytest tests/test_api.py"],
        )
        prompt = _build_prompt_from(task)

        # All sections present
        assert "You are an API developer." in prompt
        assert "Critical Facts" in prompt
        assert "Build the API." in prompt
        assert "Acceptance Criteria" in prompt
        assert "pytest tests/test_api.py" in prompt

        # Ordering: L0 → L1 → description → extras
        role_pos = prompt.index("You are an API developer.")
        facts_pos = prompt.index("Critical Facts")
        task_pos = prompt.index("Build the API.")
        criteria_pos = prompt.index("Acceptance Criteria")
        assert role_pos < facts_pos < task_pos < criteria_pos


# ======================================================================
# (g) Agent with profile but no project facts still gets L0 (+ the L1 slot)
# ======================================================================


class TestL0L1ProfileWithoutProjectFacts:
    """(g) A profile with no project-level facts still gets its L0, in tier order.

    The agent-type-scope L1 half of this case (``load_l1_facts(project_id=…,
    agent_type=profile.id)``) has no implementation on the session path —
    see the module docstring for the removed tests.
    """

    async def test_l0_from_profile_with_empty_l1_slot(self, session_orch):
        """Profile provides L0; the L1 slot is present and (while paused) empty."""
        orch = session_orch
        await create_session_project(orch)
        await create_session_profile(orch, "coding")
        write_vault_profile(orch.config, "coding", "## Role\nYou are a full-stack developer.\n")

        await _dispatch(orch, profile_id="coding")
        await _assert_launched(orch)

        doc = await render_prime(orch, "t-1")
        bodies = prime_bodies(doc)
        assert bodies["role"] == "You are a full-stack developer."
        assert "l1_facts" in bodies and bodies["l1_facts"] == ""

    async def test_l0_precedes_task_in_rendered_prime(self, session_orch):
        """Tier order survives rendering: L0 identity comes before the task body."""
        orch = session_orch
        await create_session_project(orch)
        await create_session_profile(orch, "coding")
        write_vault_profile(orch.config, "coding", "## Role\nYou are a senior engineer.\n")

        await _dispatch(orch, profile_id="coding")
        await _assert_launched(orch)

        markdown = (await render_prime(orch, "t-1")).to_markdown()
        role_pos = markdown.index("You are a senior engineer.")
        task_pos = markdown.index("Do something")
        assert role_pos < task_pos
