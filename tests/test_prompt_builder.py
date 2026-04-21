"""Tests for PromptBuilder — template loading and rendering."""

import textwrap
from pathlib import Path

import pytest

_DEFAULT_PROMPTS_DIR = Path(__file__).parent.parent / "src" / "prompts"


@pytest.fixture
def prompts_dir(tmp_path):
    """Create a temp prompts directory with test templates."""
    tpl = tmp_path / "test_identity.md"
    tpl.write_text(
        textwrap.dedent("""\
        ---
        name: test-identity
        category: system
        variables:
          - name: workspace_dir
            required: true
        ---
        You are a test agent.
        Workspace: {{workspace_dir}}
    """)
    )

    no_vars = tmp_path / "simple.md"
    no_vars.write_text(
        textwrap.dedent("""\
        ---
        name: simple
        category: task
        ---
        No variables here.
    """)
    )

    return tmp_path


def test_load_template_with_variables(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("test-identity", {"workspace_dir": "/home/user"})
    assert "You are a test agent." in result
    assert "Workspace: /home/user" in result


def test_load_template_no_variables(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("simple")
    assert "No variables here." in result


def test_load_missing_template(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("nonexistent")
    assert result is None


def test_get_raw_template(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    raw = builder.get_template("test-identity")
    assert "{{workspace_dir}}" in raw


def test_template_caching(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    r1 = builder.render_template("simple")
    r2 = builder.render_template("simple")
    assert r1 == r2


def test_set_identity(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("test-identity", {"workspace_dir": "/work"})
    system_prompt, tools = builder.build()

    assert "You are a test agent." in system_prompt
    assert "Workspace: /work" in system_prompt
    assert tools == []


def test_add_context_blocks(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.add_context("task", "Fix the login bug.")
    builder.add_context("upstream", "Auth module was completed.")
    system_prompt, _ = builder.build()

    assert "No variables here." in system_prompt
    assert "Fix the login bug." in system_prompt
    assert "Auth module was completed." in system_prompt
    # Context blocks appear in order after identity
    identity_pos = system_prompt.index("No variables here.")
    task_pos = system_prompt.index("Fix the login bug.")
    upstream_pos = system_prompt.index("Auth module was completed.")
    assert identity_pos < task_pos < upstream_pos


def test_set_tools(prompts_dir):
    from src.prompt_builder import PromptBuilder

    tools = [{"name": "create_task", "description": "Create a task"}]
    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.set_core_tools(tools)
    _, returned_tools = builder.build()

    assert returned_tools == tools


def test_empty_layers_omitted(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    # No project context, no rules, no specific context
    system_prompt, _ = builder.build()

    # Should only contain the identity text, no empty sections
    assert system_prompt.strip() == "No variables here."


def test_build_task_prompt(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.add_context("task", "Do the thing.")
    prompt = builder.build_task_prompt()

    assert isinstance(prompt, str)
    assert "No variables here." in prompt
    assert "Do the thing." in prompt


def test_add_context_section(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.add_context_section("system_info", {"workspace": "/home", "branch": "main"})
    prompt = builder.build_task_prompt()

    assert "System Info" in prompt
    assert "**workspace:** /home" in prompt
    assert "**branch:** main" in prompt


@pytest.fixture
def task_prompts_dir(tmp_path):
    """Create temp prompts dir with task-related templates."""
    (tmp_path / "plan_structure_guide.md").write_text(
        textwrap.dedent("""\
        ---
        name: plan-structure-guide
        category: task
        variables:
          - name: max_steps
            required: true
        ---
        Write a plan with at most {{max_steps}} steps.
    """)
    )

    (tmp_path / "controlled_splitting.md").write_text(
        textwrap.dedent("""\
        ---
        name: controlled-splitting
        category: task
        variables:
          - name: current_depth
            required: true
          - name: max_depth
            required: true
        ---
        You are at depth {{current_depth}} of {{max_depth}}. You may split further.
    """)
    )

    (tmp_path / "execution_focus.md").write_text(
        textwrap.dedent("""\
        ---
        name: execution-focus
        category: task
        ---
        Execute directly. Do not create plans.
    """)
    )

    return tmp_path


def test_depth_zero_gets_plan_guide(task_prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=task_prompts_dir)
    builder.add_context_section("task_depth", {"depth": 0, "max_depth": 2})
    prompt = builder.build_task_prompt()

    assert "Write a plan with at most" in prompt
    assert "Execute directly" not in prompt


def test_intermediate_depth_gets_controlled_splitting(task_prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=task_prompts_dir)
    builder.add_context_section("task_depth", {"depth": 1, "max_depth": 2})
    prompt = builder.build_task_prompt()

    assert "You are at depth 1 of 2" in prompt
    assert "Execute directly" not in prompt


def test_max_depth_gets_execution_focus(task_prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=task_prompts_dir)
    builder.add_context_section("task_depth", {"depth": 2, "max_depth": 2})
    prompt = builder.build_task_prompt()

    assert "Execute directly" in prompt
    assert "Write a plan" not in prompt


def test_build_is_idempotent(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.add_context("task", "Something.")
    r1 = builder.build()
    r2 = builder.build()
    assert r1 == r2


def test_build_task_prompt_matches_adapter_format(prompts_dir):
    """Verify PromptBuilder produces the same output as adapter._build_prompt()."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.add_context("description", "Fix the login bug in auth.py.")
    builder.add_context(
        "acceptance_criteria",
        "## Acceptance Criteria\n- Login works with valid credentials\n- Invalid creds show error",
    )
    builder.add_context(
        "test_commands",
        "## Test Commands\n- `pytest tests/test_auth.py`",
    )
    builder.add_context(
        "additional_context",
        "## Additional Context\nProject uses JWT tokens.",
    )

    prompt = builder.build_task_prompt()

    assert "Fix the login bug" in prompt
    assert "Acceptance Criteria" in prompt
    assert "pytest tests/test_auth.py" in prompt
    assert "Project uses JWT tokens" in prompt


def test_supervisor_identity_with_active_project():
    """Verify supervisor identity renders with project context appended."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=_DEFAULT_PROMPTS_DIR)
    builder.set_identity("supervisor-system", {"workspace_dir": "/home/user/.agent-queue"})
    builder.add_context(
        "active_project",
        "ACTIVE PROJECT: `my-game`. Use this as the default project_id for all tools "
        "unless the user explicitly specifies a different project.",
    )
    system_prompt, _ = builder.build()

    assert "Supervisor" in system_prompt
    assert "ACTIVE PROJECT: `my-game`" in system_prompt


def test_supervisor_identity_without_project():
    """Verify supervisor identity renders without active project."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=_DEFAULT_PROMPTS_DIR)
    builder.set_identity("supervisor-system", {"workspace_dir": "/home/user/.agent-queue"})
    system_prompt, _ = builder.build()

    assert "Supervisor" in system_prompt
    assert "ACTIVE PROJECT" not in system_prompt


def test_task_agent_assembly_ordering(prompts_dir):
    """Verify task agent prompt assembles all sections in correct order."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)

    # Layer 1: identity (use simple for test)
    builder.set_identity("simple")

    # Layer 4: system metadata
    builder.add_context(
        "system_context",
        (
            "## System Context\n"
            "- Workspace directory: /home/user/project\n"
            "- Project: my-game (id: game-001)\n"
            "- Git branch: feat/login"
        ),
    )

    # Layer 4: execution rules
    builder.add_context(
        "execution_rules",
        ("## Execution Rules\n- Do not use plan mode\n- Commit your changes when done"),
    )

    # Layer 4: upstream work
    builder.add_context(
        "upstream_work",
        (
            "## Completed Upstream Work\n"
            "### Auth Module\n"
            "**Summary:** Implemented JWT tokens.\n"
            "**Files changed:**\n- `src/auth.py`"
        ),
    )

    # Layer 4: role instructions
    builder.add_context(
        "role_instructions",
        ("## Agent Role Instructions\nYou are a backend developer specializing in security."),
    )

    # Layer 4: task description
    builder.add_context("task", ("## Task\nFix the login endpoint to validate JWT expiration."))

    prompt = builder.build_task_prompt()

    # All sections present in order
    assert "System Context" in prompt
    assert "Execution Rules" in prompt
    assert "Completed Upstream Work" in prompt
    assert "Agent Role Instructions" in prompt
    assert "Fix the login endpoint" in prompt

    # Verify ordering
    sys_pos = prompt.index("System Context")
    rules_pos = prompt.index("Execution Rules")
    upstream_pos = prompt.index("Completed Upstream Work")
    task_pos = prompt.index("Fix the login endpoint")
    assert sys_pos < rules_pos < upstream_pos < task_pos


# ------------------------------------------------------------------
# Rule loading — deprecated (playbooks spec §13 Phase 3)
# load_relevant_rules is now a no-op, retained for API compatibility.
# ------------------------------------------------------------------


def test_load_relevant_rules_is_noop(prompts_dir):
    """load_relevant_rules is a no-op after rule manager removal."""
    import asyncio
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(
        project_id="proj",
        prompts_dir=prompts_dir,
    )
    builder.set_identity("simple")
    asyncio.run(builder.load_relevant_rules("anything"))
    system_prompt, _ = builder.build()

    # No rules section should appear — load_relevant_rules is a no-op
    assert "Applicable Rules" not in system_prompt


# ------------------------------------------------------------------
# L0 Identity tier — role extraction and injection
# ------------------------------------------------------------------


def test_extract_section_finds_role():
    """extract_section returns content under ## Role."""
    from src.prompt_builder import extract_section

    md = textwrap.dedent("""\
        # Coding Agent

        ## Role
        You are a software engineering agent. You write, modify, and debug code
        within a project workspace.

        ## Config
        ```json
        {"model": "claude-sonnet-4-6"}
        ```
    """)

    result = extract_section(md, "Role")
    assert result is not None
    assert "software engineering agent" in result
    assert "Config" not in result


def test_extract_section_returns_none_when_missing():
    """extract_section returns None when heading is not found."""
    from src.prompt_builder import extract_section

    md = "# Agent\n\n## Config\nSome config."
    assert extract_section(md, "Role") is None


def test_extract_section_case_insensitive():
    """extract_section matches headings case-insensitively."""
    from src.prompt_builder import extract_section

    md = "## role\nYou are a test agent."
    result = extract_section(md, "Role")
    assert result == "You are a test agent."


def test_extract_section_at_end_of_file():
    """extract_section handles section at end of file (no next heading)."""
    from src.prompt_builder import extract_section

    md = "## Overview\nSome overview.\n\n## Role\nYou are the last section."
    result = extract_section(md, "Role")
    assert result == "You are the last section."


def test_extract_section_empty_body():
    """extract_section returns None for headings with empty body."""
    from src.prompt_builder import extract_section

    md = "## Role\n\n## Config\nStuff."
    assert extract_section(md, "Role") is None


def test_set_l0_role(prompts_dir):
    """set_l0_role injects text at the very start of the prompt."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l0_role("You are a coding agent.")
    builder.set_identity("simple")
    builder.add_context("task", "Fix a bug.")

    prompt = builder.build_task_prompt()

    # L0 role appears before identity and context
    role_pos = prompt.index("You are a coding agent.")
    identity_pos = prompt.index("No variables here.")
    task_pos = prompt.index("Fix a bug.")
    assert role_pos < identity_pos < task_pos


def test_set_l0_role_empty_noop(prompts_dir):
    """set_l0_role with empty string does not add to prompt."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l0_role("")
    builder.set_l0_role("   ")
    builder.set_identity("simple")

    prompt = builder.build_task_prompt()
    assert prompt.strip() == "No variables here."


def test_set_l0_role_from_markdown(prompts_dir):
    """set_l0_role_from_markdown concatenates Role + Rules + Reflection.

    Section headings are preserved so the LLM can distinguish identity
    (Role) from behavioural constraints (Rules) from post-task
    guidance (Reflection).  Matches the ``system_prompt_suffix``
    format produced by the profile parser for Claude Code agents.
    """
    from src.prompt_builder import PromptBuilder

    profile_md = textwrap.dedent("""\
        ---
        id: coding
        name: Coding Agent
        ---

        # Coding Agent

        ## Role
        You are a software engineering agent. You write clean code.

        ## Rules
        - Always run tests before committing

        ## Reflection
        After each task, note what worked and what didn't.
    """)

    builder = PromptBuilder(prompts_dir=prompts_dir)
    found = builder.set_l0_role_from_markdown(profile_md)
    assert found is True

    prompt = builder.build_task_prompt()
    # All three sections flow into the prompt, with their headings.
    assert "## Role" in prompt
    assert "## Rules" in prompt
    assert "## Reflection" in prompt
    assert "software engineering agent" in prompt
    assert "Always run tests" in prompt
    assert "what worked and what didn't" in prompt


def test_set_l0_role_from_markdown_rules_only(prompts_dir):
    """Returns True when Rules is present even if Role is missing."""
    from src.prompt_builder import PromptBuilder

    profile_md = textwrap.dedent("""\
        # Agent

        ## Rules
        - Rule only
    """)
    builder = PromptBuilder(prompts_dir=prompts_dir)
    found = builder.set_l0_role_from_markdown(profile_md)
    assert found is True

    prompt = builder.build_task_prompt()
    assert "## Rules" in prompt
    assert "Rule only" in prompt


def test_set_l0_role_from_markdown_no_role(prompts_dir):
    """set_l0_role_from_markdown returns False when none of the three sections exist."""
    from src.prompt_builder import PromptBuilder

    profile_md = "# Agent\n\n## Config\nSome config."
    builder = PromptBuilder(prompts_dir=prompts_dir)
    found = builder.set_l0_role_from_markdown(profile_md)
    assert found is False


def test_l0_role_ordering_in_full_assembly(prompts_dir):
    """L0 role appears before all other layers in full assembly."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l0_role("You are a QA agent.")
    builder.set_identity("simple")
    builder.add_context("system_context", "## System Context\n- Workspace: /home")
    builder.add_context("task", "## Task\nRun tests.")

    prompt = builder.build_task_prompt()

    role_pos = prompt.index("You are a QA agent.")
    sys_pos = prompt.index("System Context")
    task_pos = prompt.index("Run tests.")
    assert role_pos < sys_pos < task_pos


# ------------------------------------------------------------------
# Override content — project-specific overrides (§5)
# ------------------------------------------------------------------


def test_set_override_content(prompts_dir):
    """set_override_content injects override text after L0 role."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l0_role("You are a coding agent.")
    builder.set_override_content("Use composition over inheritance.")
    builder.set_identity("simple")
    builder.add_context("task", "Fix a bug.")

    prompt = builder.build_task_prompt()

    assert "Use composition over inheritance." in prompt
    # Override appears between L0 role and identity
    role_pos = prompt.index("You are a coding agent.")
    override_pos = prompt.index("Use composition over inheritance.")
    identity_pos = prompt.index("No variables here.")
    task_pos = prompt.index("Fix a bug.")
    assert role_pos < override_pos < identity_pos < task_pos


def test_set_override_content_strips_frontmatter(prompts_dir):
    """set_override_content strips YAML frontmatter from override files."""
    from src.prompt_builder import PromptBuilder

    override_md = textwrap.dedent("""\
        ---
        tags: [override, coding, mech-fighters]
        agent_type: coding
        ---

        # Coding Agent Overrides — Mech Fighters

        Use composition over inheritance.
    """)

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_override_content(override_md)

    prompt = builder.build_task_prompt()

    assert "Coding Agent Overrides" in prompt
    assert "Use composition over inheritance." in prompt
    # Frontmatter should be stripped
    assert "tags:" not in prompt
    assert "agent_type: coding" not in prompt


def test_set_override_content_empty_noop(prompts_dir):
    """set_override_content with empty string does not add to prompt."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_override_content("")
    builder.set_override_content("   ")
    builder.set_identity("simple")

    prompt = builder.build_task_prompt()
    assert prompt.strip() == "No variables here."


def test_set_override_content_without_l0_role(prompts_dir):
    """set_override_content works even without L0 role set."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_override_content("Use ECS framework only.")
    builder.set_identity("simple")
    builder.add_context("task", "Add a feature.")

    prompt = builder.build_task_prompt()

    assert "Use ECS framework only." in prompt
    # Override appears before identity and context
    override_pos = prompt.index("Use ECS framework only.")
    identity_pos = prompt.index("No variables here.")
    task_pos = prompt.index("Add a feature.")
    assert override_pos < identity_pos < task_pos


def test_override_content_full_assembly_ordering(prompts_dir):
    """Override content sits between L0 role and identity in full assembly."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l0_role("You are a QA agent.")
    builder.set_override_content("# Project Overrides\nPrefer integration tests.")
    builder.set_identity("simple")
    builder.add_context("system_context", "## System Context\n- Workspace: /home")
    builder.add_context("task", "## Task\nRun tests.")

    prompt = builder.build_task_prompt()

    role_pos = prompt.index("You are a QA agent.")
    override_pos = prompt.index("Prefer integration tests.")
    identity_pos = prompt.index("No variables here.")
    sys_pos = prompt.index("System Context")
    task_pos = prompt.index("Run tests.")
    assert role_pos < override_pos < identity_pos < sys_pos < task_pos


def test_set_override_content_frontmatter_only(prompts_dir):
    """Override file with only frontmatter and no body is treated as empty."""
    from src.prompt_builder import PromptBuilder

    override_md = "---\ntags: [override]\n---\n"
    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_override_content(override_md)
    builder.set_identity("simple")

    prompt = builder.build_task_prompt()
    assert prompt.strip() == "No variables here."


# ------------------------------------------------------------------
# L1 Critical Facts tier — KV facts injection
# ------------------------------------------------------------------


def test_set_l1_facts(prompts_dir):
    """set_l1_facts injects text after L0 role and before identity."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l0_role("You are a coding agent.")
    builder.set_l1_facts("## Critical Facts\n- tech_stack: Python\n- test_command: pytest")
    builder.set_identity("simple")
    builder.add_context("task", "Fix a bug.")

    prompt = builder.build_task_prompt()

    # All sections present
    assert "You are a coding agent." in prompt
    assert "Critical Facts" in prompt
    assert "tech_stack: Python" in prompt
    assert "test_command: pytest" in prompt
    assert "No variables here." in prompt
    assert "Fix a bug." in prompt

    # L0 < L1 < identity < context
    role_pos = prompt.index("You are a coding agent.")
    facts_pos = prompt.index("Critical Facts")
    identity_pos = prompt.index("No variables here.")
    task_pos = prompt.index("Fix a bug.")
    assert role_pos < facts_pos < identity_pos < task_pos


def test_set_l1_facts_empty_noop(prompts_dir):
    """set_l1_facts with empty string does not add to prompt."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l1_facts("")
    builder.set_l1_facts("   ")
    builder.set_identity("simple")

    prompt = builder.build_task_prompt()
    assert "Critical Facts" not in prompt
    assert prompt.strip() == "No variables here."


def test_set_l1_facts_without_l0(prompts_dir):
    """L1 facts work even when L0 role is not set."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l1_facts("## Critical Facts\n- deploy_branch: main")
    builder.set_identity("simple")

    prompt = builder.build_task_prompt()

    facts_pos = prompt.index("Critical Facts")
    identity_pos = prompt.index("No variables here.")
    assert facts_pos < identity_pos


def test_l1_facts_ordering_in_full_assembly(prompts_dir):
    """L1 facts appear after L0 role and override, before all other layers."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_l0_role("You are a QA agent.")
    builder.set_l1_facts("## Critical Facts\n- lang: Python")
    builder.set_identity("simple")
    builder.add_context("system_context", "## System Context\n- Workspace: /home")
    builder.add_context("task", "## Task\nRun tests.")

    prompt = builder.build_task_prompt()

    role_pos = prompt.index("You are a QA agent.")
    facts_pos = prompt.index("Critical Facts")
    identity_pos = prompt.index("No variables here.")
    sys_pos = prompt.index("System Context")
    task_pos = prompt.index("Run tests.")
    assert role_pos < facts_pos < identity_pos < sys_pos < task_pos
