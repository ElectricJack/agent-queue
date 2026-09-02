"""Golden tests for the prime renderer (``src/prime/``).

Covers docs/specs/implementation/aq-surface.md §10.1: "renderer golden tests
(fixture vault + task -> expected markdown); override template;
memory-paused slots empty". See also design §5.2 (canonical section order)
and §5.3 (``.aq/PRIME.md`` override).
"""

from __future__ import annotations

import json
import os
import time

import pytest

from src.config import AppConfig
from src.models import AgentProfile, Project, SessionRecord, Task
from src.prime import PrimeRenderer
from src.prime.models import PrimeDocument, PrimeSection

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    from src.database.adapters.sqlite import SQLiteDatabaseAdapter

    adapter = SQLiteDatabaseAdapter(":memory:")
    await adapter.initialize()
    yield adapter
    await adapter.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(data_dir=str(tmp_path / "data"))


@pytest.fixture
async def task(db):
    await db.create_project(Project(id="proj-1", name="Test Project"))
    await db.create_profile(AgentProfile(id="coder", name="Coder"))
    t = Task(
        id="task-1",
        project_id="proj-1",
        title="Fix the bug",
        description="Do the thing, carefully.",
        profile_id="coder",
    )
    await db.create_task(t)
    return t


def _write(path, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Golden: full default assembly
# ---------------------------------------------------------------------------


class TestGoldenAssembly:
    async def test_project_default_profile_supplies_role_sections(self, db, config):
        """A dispatched task may inherit its profile without storing it on the task row."""
        await db.create_profile(AgentProfile(id="coder", name="Coder"))
        await db.create_project(
            Project(id="default-profile-project", name="Default Profile", default_profile_id="coder")
        )
        await db.create_task(
            Task(
                id="default-profile-task",
                project_id="default-profile-project",
                title="Inherited profile",
                description="",
            )
        )
        _write(
            os.path.join(config.vault_agent_types, "coder", "profile.md"),
            "## Role\nYou are the default coder.\n",
        )
        _write(
            os.path.join(
                config.vault_projects,
                "default-profile-project",
                "agent-types",
                "coder",
                "profile.md",
            ),
            "## Role\nUse the project conventions.\n",
        )

        doc = await PrimeRenderer(db, config).render_for_task("default-profile-task")
        by_key = {section.key: section.body for section in doc.sections}

        assert by_key["role"] == "You are the default coder."
        assert by_key["project_role"] == "Use the project conventions."

    async def test_role_and_project_role_sections_from_vault_files(self, db, config, task):
        _write(
            os.path.join(config.vault_agent_types, "coder", "profile.md"),
            "## Role\nYou are a careful coder.\n\n## Rules\nAlways test.\n",
        )
        _write(
            os.path.join(
                config.vault_projects, "proj-1", "agent-types", "coder", "profile.md"
            ),
            "## Role\nOn this project, prefer small PRs.\n",
        )

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        by_key = {s.key: s for s in doc.sections}

        assert by_key["role"].body == "You are a careful coder.\n\n### Rules\nAlways test."
        assert by_key["project_role"].body == "On this project, prefer small PRs."

        markdown = doc.to_markdown()
        assert "## Role" in markdown
        assert "## Project Role Override" in markdown
        assert "You are a careful coder." in markdown
        assert "Always test." in markdown
        assert "On this project, prefer small PRs." in markdown

    async def test_project_override_carries_rules_too(self, db, config, task):
        _write(
            os.path.join(config.vault_agent_types, "coder", "profile.md"),
            "## Role\nBase role.\n",
        )
        _write(
            os.path.join(
                config.vault_projects, "proj-1", "agent-types", "coder", "profile.md"
            ),
            "## Role\nProject role.\n\n## Rules\nProject rule.\n",
        )

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        by_key = {s.key: s for s in doc.sections}
        assert by_key["project_role"].body == "Project role.\n\n### Rules\nProject rule."

    async def test_profile_without_rules_renders_role_only(self, db, config, task):
        _write(
            os.path.join(config.vault_agent_types, "coder", "profile.md"),
            "## Role\nJust a role.\n\n## Config\n```json\n{}\n```\n",
        )

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        by_key = {s.key: s for s in doc.sections}
        assert by_key["role"].body == "Just a role."
        assert "### Rules" not in doc.to_markdown()

    async def test_machine_only_profile_headings_never_reach_the_agent(self, db, config, task):
        _write(
            os.path.join(config.vault_agent_types, "coder", "profile.md"),
            "## Role\nR.\n\n## Config\nCONFIG-SECRET\n\n## Tools\nTOOLS-BLOB\n"
            "\n## MCP Servers\nMCP-BLOB\n\n## Reflection\nREFLECT-BLOB\n"
            "\n## Rules\nRULE-ONE\n",
        )

        markdown = (await PrimeRenderer(db, config).render_for_task("task-1")).to_markdown()
        assert "RULE-ONE" in markdown
        for machine_only in ("CONFIG-SECRET", "TOOLS-BLOB", "MCP-BLOB", "REFLECT-BLOB"):
            assert machine_only not in markdown

    async def test_rules_only_profile_labels_its_rules(self, db, config, task):
        _write(
            os.path.join(config.vault_agent_types, "coder", "profile.md"),
            "## Rules\nOnly a rule.\n",
        )

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        by_key = {s.key: s for s in doc.sections}
        assert by_key["role"].body == "### Rules\nOnly a rule."

    async def test_missing_profile_files_render_empty_and_are_omitted(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        by_key = {s.key: s for s in doc.sections}
        assert by_key["role"].body == ""
        assert by_key["project_role"].body == ""
        assert "## Role" not in doc.to_markdown()
        assert "## Project Role Override" not in doc.to_markdown()

    async def test_task_section_carries_id_title_status_description(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task"]
        assert "task-1" in body
        assert "Fix the bug" in body
        assert "DEFINED" in body
        assert "Do the thing, carefully." in body

    async def test_section_order_is_canonical(self, db, config, task):
        from src.prime.models import SECTION_KEYS

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        assert tuple(s.key for s in doc.sections) == SECTION_KEYS

    async def test_unknown_task_raises_value_error(self, db, config):
        with pytest.raises(ValueError, match="not found"):
            await PrimeRenderer(db, config).render_for_task("nope")

    async def test_missing_task_id_raises_value_error(self, db, config):
        with pytest.raises(ValueError, match="required"):
            await PrimeRenderer(db, config).render_for_task("")

    async def test_tokens_est_is_chars_over_four(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        assert doc.tokens_est() == len(doc.to_markdown()) // 4


# ---------------------------------------------------------------------------
# Task context: notes, attachments, spec_ref inlining, handoff exclusion
# ---------------------------------------------------------------------------


class TestTaskContextSection:
    async def test_note_context_row_is_inlined(self, db, config, task):
        await db.add_task_context("task-1", type="note", label="note", content="hello there")
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task_context"]
        assert "hello there" in body

    async def test_handoff_rows_excluded_from_task_context_section(self, db, config, task):
        await db.add_task_context(
            "task-1", type="handoff", label="handoff", content=json.dumps({"subject": "x"})
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task_context"]
        assert body == ""

    async def test_attachments_listed(self, db, config):
        await db.create_project(Project(id="proj-1", name="P"))
        t = Task(
            id="task-2",
            project_id="proj-1",
            title="T",
            description="",
            attachments=["/tmp/a.png", "/tmp/b.png"],
        )
        await db.create_task(t)
        doc = await PrimeRenderer(db, config).render_for_task("task-2")
        body = {s.key: s.body for s in doc.sections}["task_context"]
        assert "/tmp/a.png" in body
        assert "/tmp/b.png" in body

    async def test_spec_ref_resolves_and_inlines_section(self, db, config, task):
        spec_rel = os.path.join("projects", "proj-1", "specs", "widget.md")
        _write(
            os.path.join(config.vault_root, spec_rel),
            "## 1. Overview\nIntro text.\n\n## 3. Schema\nThe schema body goes here.\n",
        )
        await db.add_task_context(
            "task-1",
            type="spec_ref",
            label="spec",
            content=json.dumps({"path": spec_rel, "section": "3. Schema"}),
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task_context"]
        assert "The schema body goes here." in body
        assert "Intro text." not in body  # only the referenced heading is inlined

    async def test_spec_ref_missing_file_degrades_gracefully(self, db, config, task):
        await db.add_task_context(
            "task-1",
            type="spec_ref",
            label="spec",
            content=json.dumps({"path": "projects/proj-1/specs/nope.md", "section": "1. X"}),
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task_context"]
        assert "unresolved" in body
        assert "file not found" in body

    async def test_spec_ref_missing_heading_degrades_gracefully(self, db, config, task):
        spec_rel = os.path.join("projects", "proj-1", "specs", "widget.md")
        _write(os.path.join(config.vault_root, spec_rel), "## 1. Overview\nIntro.\n")
        await db.add_task_context(
            "task-1",
            type="spec_ref",
            label="spec",
            content=json.dumps({"path": spec_rel, "section": "9. Nonexistent"}),
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task_context"]
        assert "unresolved" in body
        assert "heading not found" in body


class TestSpecRefContainment:
    """``_render_spec_ref`` inlines the referenced file into another agent's
    prompt — the whole file when no ``section`` is given. Containment is
    enforced here as well as in the graph validator, because a row can reach
    ``task_context`` by paths other than a validated graph.
    """

    SECRET = "sk-do-not-leak"

    @pytest.fixture
    def secret_file(self, tmp_path):
        target = tmp_path / "outside" / "secret.md"
        _write(str(target), f"## Secret\n{self.SECRET}\n")
        return target

    async def _context_body(self, db, config, ref: dict) -> str:
        await db.add_task_context(
            "task-1", type="spec_ref", label="spec", content=json.dumps(ref)
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        return {s.key: s.body for s in doc.sections}["task_context"]

    async def test_dotdot_traversal_is_refused(self, db, config, task, secret_file):
        rel = os.path.relpath(secret_file, config.vault_root)
        body = await self._context_body(db, config, {"path": rel, "section": "Secret"})
        assert self.SECRET not in body
        assert "refused" in body

    async def test_absolute_path_outside_the_vault_is_refused(self, db, config, task, secret_file):
        body = await self._context_body(db, config, {"path": str(secret_file)})
        assert self.SECRET not in body
        assert "refused" in body

    async def test_whole_file_inlining_is_refused_too(self, db, config, task, secret_file):
        """No ``section`` means the *entire* file would be inlined."""
        rel = os.path.relpath(secret_file, config.vault_root)
        body = await self._context_body(db, config, {"path": rel})
        assert self.SECRET not in body

    async def test_symlink_out_of_the_vault_is_refused(self, db, config, task, secret_file):
        link = os.path.join(config.vault_root, "projects", "proj-1", "specs", "escape.md")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        try:
            os.symlink(secret_file, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this host")
        body = await self._context_body(
            db, config, {"path": "projects/proj-1/specs/escape.md"}
        )
        assert self.SECRET not in body
        assert "refused" in body

    async def test_in_vault_reference_is_unaffected(self, db, config, task):
        spec_rel = os.path.join("projects", "proj-1", "specs", "widget.md")
        _write(os.path.join(config.vault_root, spec_rel), "## 3. Schema\nSchema body.\n")
        body = await self._context_body(
            db, config, {"path": spec_rel, "section": "3. Schema"}
        )
        assert "Schema body." in body


# ---------------------------------------------------------------------------
# Workspaces section
# ---------------------------------------------------------------------------


class TestWorkspacesSection:
    async def test_work_dir_from_task_set_metadata(self, db, config, task):
        await db.set_task_meta("task-1", "work_dir", "/work/task-1")
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["workspaces"]
        assert "/work/task-1" in body

    async def test_explicit_work_dir_overrides_metadata(self, db, config, task):
        await db.set_task_meta("task-1", "work_dir", "/from/meta")
        doc = await PrimeRenderer(db, config).render_for_task("task-1", work_dir="/from/arg")
        body = {s.key: s.body for s in doc.sections}["workspaces"]
        assert "/from/arg" in body
        assert "/from/meta" not in body

    async def test_branch_and_pr_url_included(self, db, config):
        await db.create_project(Project(id="proj-1", name="P"))
        t = Task(
            id="task-3",
            project_id="proj-1",
            title="T",
            description="",
            branch_name="feat/x",
            pr_url="https://example/pr/1",
        )
        await db.create_task(t)
        doc = await PrimeRenderer(db, config).render_for_task("task-3")
        body = {s.key: s.body for s in doc.sections}["workspaces"]
        assert "feat/x" in body
        assert "https://example/pr/1" in body


# ---------------------------------------------------------------------------
# Messages + handoff section
# ---------------------------------------------------------------------------


class TestMessagesSection:
    async def test_no_messages_table_no_handoff_renders_empty(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["messages"]
        assert body == ""

    async def test_latest_handoff_note_is_rendered(self, db, config, task):
        await db.add_task_context(
            "task-1",
            type="handoff",
            label="handoff",
            content=json.dumps({"subject": "first", "detail": "old"}),
        )
        await db.add_task_context(
            "task-1",
            type="handoff",
            label="handoff",
            content=json.dumps({"subject": "second", "detail": "latest detail"}),
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["messages"]
        assert "second" in body
        assert "latest detail" in body


# ---------------------------------------------------------------------------
# Memory-paused slots (design §5.2 #7-8, feature-pauses.md)
# ---------------------------------------------------------------------------


class TestMemoryPausedSlots:
    async def test_l1_and_l2_render_empty_while_memory_paused(self, db, config, task):
        assert config.memory.enabled is False  # sanity: pause is the default
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        by_key = {s.key: s.body for s in doc.sections}
        assert by_key["l1_facts"] == ""
        assert by_key["l2_context"] == ""
        assert "## Facts" not in doc.to_markdown()
        assert "## Topic Context" not in doc.to_markdown()

    async def test_l1_and_l2_slots_still_present_as_section_vars(self, db, config, task):
        # The slots exist as template variables even when empty (design §5.2)
        # so a future memory comeback is a renderer change, not a protocol one.
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        variables = doc.section_vars()
        assert "l1_facts" in variables
        assert "l2_context" in variables


# ---------------------------------------------------------------------------
# Static templates: tool guidance + completion protocol
# ---------------------------------------------------------------------------


class TestStaticSections:
    async def test_tool_guidance_mentions_cli_and_nine_tools(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["tool_guidance"]
        assert "aq " in body
        for name in ("task_show", "task_set", "memory_search"):
            assert name in body

    async def test_completion_protocol_embeds_task_id(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["completion_protocol"]
        assert "aq task close task-1" in body
        assert "aq session drain-ack" in body

    @pytest.mark.parametrize("lifecycle", [None, "pool"])
    async def test_completion_protocol_renders_emergent_work_guidance(
        self, db, config, task, lifecycle
    ):
        session_id = None
        if lifecycle == "pool":
            session_id = "pool-session"
            await db.create_session(
                SessionRecord(
                    id=session_id,
                    project_id="proj-1",
                    profile_id="coder",
                    harness="codex",
                    provider="fake",
                    name="pool-session",
                    lifecycle="pool",
                    work_dir="/tmp/pool-session",
                    epoch="test",
                    instance_token="test-only",
                    started_at=time.time(),
                    task_id="task-1",
                    state="running",
                )
            )
        doc = await PrimeRenderer(db, config).render_for_task("task-1", session_id=session_id)
        body = {s.key: s.body for s in doc.sections}["completion_protocol"]
        assert "## Emergent work" in body
        assert "aq task create" in body
        assert "--reason" in body
        assert "discovered-from" in body
        assert "--parent <container-id>" in body

    async def test_emergent_work_is_omitted_when_the_profile_denies_create_task(
        self, db, config, task
    ):
        """A profile-owned capability gate would deny the very command we ask for.

        ``create_task`` is on the scope allowlist, but the profile's
        ``aq_commands`` is a second gate — telling a session to file emergent
        work it cannot file just produces a capability denial.
        """
        await db.update_profile(
            "coder",
            aq_commands=["task_close", "task_show"],
            harness_tools=["Bash", "Read"],
            plugin_tools=[],
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["completion_protocol"]
        assert "## Emergent work" not in body
        # The rest of the completion protocol is untouched.
        assert "aq task close task-1" in body

    async def test_emergent_work_renders_when_the_profile_allows_create_task(
        self, db, config, task
    ):
        await db.update_profile(
            "coder",
            aq_commands=["create_task", "task_close"],
            harness_tools=["Bash", "Read"],
            plugin_tools=[],
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["completion_protocol"]
        assert "## Emergent work" in body

    async def test_emergent_work_survives_an_unresolvable_profile(self, db, config):
        """Fail open: prime cannot ask the gate, so it keeps the instruction."""
        await db.create_project(Project(id="proj-2", name="No Profile"))
        await db.create_task(
            Task(id="task-2", project_id="proj-2", title="No profile", description="")
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-2")
        body = {s.key: s.body for s in doc.sections}["completion_protocol"]
        assert "## Emergent work" in body


# ---------------------------------------------------------------------------
# .aq/PRIME.md override (design §5.3)
# ---------------------------------------------------------------------------


class TestOverride:
    async def test_override_replaces_default_body_entirely(self, db, config, task, tmp_path):
        work_dir = tmp_path / "work"
        _write(
            str(work_dir / ".aq" / "PRIME.md"),
            "CUSTOM START\n\n{{task}}\n\n{{tool_guidance}}\n\nCUSTOM END",
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-1", work_dir=str(work_dir))
        assert doc.source == "override:.aq/PRIME.md"
        markdown = doc.to_markdown()
        assert markdown.startswith("CUSTOM START")
        assert markdown.rstrip().endswith("CUSTOM END")
        assert "task-1" in markdown  # {{task}} substituted
        assert "## Role" not in markdown  # default assembly is fully replaced

    async def test_no_override_file_uses_default_source(self, db, config, task, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        doc = await PrimeRenderer(db, config).render_for_task("task-1", work_dir=str(work_dir))
        assert doc.source == "default"

    async def test_override_unknown_variable_is_blank_not_an_error(self, db, config, task, tmp_path):
        work_dir = tmp_path / "work"
        _write(str(work_dir / ".aq" / "PRIME.md"), "before[{{nonexistent_var}}]after")
        doc = await PrimeRenderer(db, config).render_for_task("task-1", work_dir=str(work_dir))
        assert doc.to_markdown() == "before[]after"

    async def test_override_extra_vars_task_id_work_dir_branch(self, db, config, tmp_path):
        await db.create_project(Project(id="proj-1", name="P"))
        t = Task(
            id="task-4", project_id="proj-1", title="T", description="", branch_name="feat/y"
        )
        await db.create_task(t)
        work_dir = tmp_path / "work"
        _write(
            str(work_dir / ".aq" / "PRIME.md"),
            "{{task.id}} | {{work_dir}} | {{branch}}",
        )
        doc = await PrimeRenderer(db, config).render_for_task("task-4", work_dir=str(work_dir))
        assert doc.to_markdown() == f"task-4 | {work_dir} | feat/y"


# ---------------------------------------------------------------------------
# PrimeDocument.to_markdown / section_vars — pure model tests
# ---------------------------------------------------------------------------


class TestPrimeDocumentModel:
    async def test_empty_sections_produce_empty_markdown(self):
        doc = PrimeDocument(
            task_id="t1",
            session_id=None,
            sections=(PrimeSection(key="role", title="Role", body=""),),
            source="default",
            rendered_at=__import__("datetime").datetime.now(),
        )
        assert doc.to_markdown() == ""
        assert doc.tokens_est() == 0

    async def test_section_vars_includes_all_keys_plus_extras(self):
        doc = PrimeDocument(
            task_id="t1",
            session_id=None,
            sections=(
                PrimeSection(key="role", title="Role", body="R"),
                PrimeSection(key="task", title="Task", body="T"),
            ),
            source="default",
            rendered_at=__import__("datetime").datetime.now(),
            work_dir="/wd",
            branch="main",
        )
        variables = doc.section_vars()
        assert variables["role"] == "R"
        assert variables["task"] == "T"
        assert variables["task.id"] == "t1"
        assert variables["work_dir"] == "/wd"
        assert variables["branch"] == "main"
