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
from pathlib import Path

import pytest

from tests.db_fixtures import lease_dsn

from src.config import AppConfig, DatabaseConfig
from src.models import AgentProfile, Project, ProjectStatus, SessionRecord, Task, TaskType
from src.prime import PrimeRenderer
from src.prime.models import PrimeDocument, PrimeSection
from src.prime.sections import build_role_section
from src.profiles.parser import parse_profile

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(reuse_database):
    adapter = await reuse_database("prime")
    yield adapter


@pytest.fixture
def config(tmp_path):
    return AppConfig(database=DatabaseConfig(url=lease_dsn("prime")), data_dir=str(tmp_path / "data"))


@pytest.fixture
async def task(db):
    await db.create_project(Project(id="proj-1", name="Test Project"))
    await db.create_profile(AgentProfile(id="coder", name="Coder"))
    t = Task(
        id="task-1",
        project_id="proj-1",
        title="Fix the bug",
        description="Do the thing, carefully.",
        profile_id="coder", route_source="legacy",
    )
    await db.create_task(t)
    return t


def _write(path, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


@pytest.mark.parametrize("override", [False, True])
async def test_paused_project_instruction_survives_local_override(db, config, task, tmp_path, override):
    await db.update_project(task.project_id, status=ProjectStatus.PAUSED)
    if override:
        _write(str(tmp_path / ".aq" / "PRIME.md"), "Local task instructions.")
    renderer = PrimeRenderer(db, config)
    markdown = (await renderer.render_for_task(task.id, work_dir=str(tmp_path))).to_markdown()
    assert "## Project paused" in markdown
    assert "next safe point" in markdown
    assert "pending wait completing" in markdown
    await db.update_project(task.project_id, status=ProjectStatus.ACTIVE)
    resumed = (await renderer.render_for_task(task.id, work_dir=str(tmp_path))).to_markdown()
    assert "## Project paused" not in resumed


# ---------------------------------------------------------------------------
# Golden: full default assembly
# ---------------------------------------------------------------------------


class TestGoldenAssembly:
    @pytest.mark.parametrize("profile_id", ["supervisor", "coder"])
    async def test_installed_supervisor_receives_infrastructure_policy(self, config, profile_id):
        path = Path(config.vault_agent_types) / profile_id / "profile.md"
        _write(path, "## Role\nOperator-customized role.\n")

        section = await build_role_section(config, profile_id)

        assert "Operator-customized role." in section.body
        assert ("Never mass-pause" in section.body) == (profile_id == "supervisor")
        assert ("with the train off, train controls" in section.body) == (
            profile_id == "supervisor"
        )
        assert path.read_text() == "## Role\nOperator-customized role.\n"

    async def test_an_unrouted_task_takes_no_role_sections(self, db, config):
        """An unrouted task takes no role from any profile (mandatory routing §8)."""
        await db.create_profile(AgentProfile(id="coder", name="Coder"))
        await db.create_project(Project(id="default-profile-project", name="Default Profile"))
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

        assert "You are the default coder." not in by_key.get("role", "")
        assert "Use the project conventions." not in by_key.get("project_role", "")

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

    async def test_research_task_lists_its_implicit_review_deliverable(self, db, config, task):
        """The worker learns up front that the document goes to Reviews (vivid-delta)."""
        await db.update_task("task-1", task_type=TaskType.RESEARCH)
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task"]
        assert "## Deliverables" in body
        assert "- [review] `review` — `any`" in body
        assert "aq review submit --task-id task-1" in body
        assert "do not commit it" in body

    async def test_task_without_a_review_item_gets_no_review_guidance(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task"]
        assert "## Deliverables" not in body
        assert "aq review submit" not in body

    @pytest.mark.parametrize("bounded", [False, True])
    async def test_revision_task_gets_publication_guidance_without_a_document_type(
        self, db, config, task, bounded
    ):
        await db.set_task_meta(task.id, "review_response", {"review_id": "rev-assigned", "revision": 2})
        await db.update_task(task.id, description="Long context. " * 2000)
        doc = await PrimeRenderer(db, config).render_for_task(task.id, bounded_worker=bounded)
        body = {s.key: s.body for s in doc.sections}["task"]
        assert "## Review revision publication" in body
        assert "newer than revision 2 before a passing close" in body
        assert "aq review show --review-id rev-assigned --comments" in body
        assert "aq review submit --review-id rev-assigned --file <draft.md>" in body
        assert "do not edit the vault mirror" in body
        assert "--resolves" in body
        assert "content hash" in body
        assert "Do not wait for approval" in body

    async def test_task_section_omits_subtasks_block_when_none_exist(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task"]
        assert "## Subtasks" not in body

    async def test_task_section_renders_subtasks_with_status_glyphs_in_order(
        self, db, config, task
    ):
        await db.add_task_subtasks(
            "task-1",
            "proj-1",
            [{"title": "First"}, {"title": "Second"}, {"title": "Third"}, {"title": "Fourth"}],
        )
        await db.update_task_subtask("task-1", 1, status="done")
        await db.update_task_subtask("task-1", 2, status="skipped", note="n/a")
        await db.update_task_subtask("task-1", 3, status="in_progress")

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task"]

        assert "## Subtasks" in body
        assert "- [x] 1. First" in body
        assert "- [-] 2. Second" in body
        assert "- [~] 3. Third" in body
        assert "- [ ] 4. Fourth" in body
        assert "Report progress as you go: `aq task subtask-done N`." in body
        assert "Full context for one: `aq task subtask-show N`." in body
        assert "Work them in order unless the task says otherwise." in body

    async def test_subtasks_list_without_the_progress_line_when_updates_are_denied(
        self, db, config, task
    ):
        """An install upgraded from before the grants existed still shows the
        checklist, but is not told to run a command its own policy denies."""
        await db.update_profile(
            "coder",
            aq_commands=["task_close", "task_subtasks"],
            harness_tools=["Bash", "Read"],
            plugin_tools=[],
        )
        await db.add_task_subtasks("task-1", "proj-1", [{"title": "First"}])

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task"]

        assert "## Subtasks" in body
        assert "- [ ] 1. First" in body
        assert "Report progress as you go" not in body
        assert "Work them in order unless the task says otherwise." in body

    async def test_the_progress_line_returns_once_the_profile_grants_the_update(
        self, db, config, task
    ):
        await db.update_profile(
            "coder",
            aq_commands=["task_close", "task_subtask_update"],
            harness_tools=["Bash", "Read"],
            plugin_tools=[],
        )
        await db.add_task_subtasks("task-1", "proj-1", [{"title": "First"}])

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["task"]
        assert "Report progress as you go: `aq task subtask-done N`." in body

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
    @pytest.mark.parametrize("override", [False, True])
    async def test_aggregate_budget_pages_salvage_without_cutting_inline_rules(
        self, db, config, task, tmp_path, override,
    ):
        from src.knowledge.budget import ContextBudget

        # Exercise the supported legacy operator cap, independently of auto sizing.
        config.knowledge.context.input_max_tokens = 32768
        description = "Required acceptance αβ.\n" * 3000 + "FINAL ACCEPTANCE"
        salvage = "diff --git a/work.py b/work.py\n" + "+retained αβ\n" * 10000 + "FINAL DIFF"
        await db.update_task(task.id, description=description, deliverables=json.dumps([
            {"kind": "file", "id": "code", "target": "src/work.py"},
        ]))
        await db.add_task_context(task.id, type="worktree_salvage", label="Saved work", content=salvage)
        await db.add_task_context(task.id, type="handoff", label="handoff", content=json.dumps({
            "constraints": ["Keep the human acceptance gate."],
            "next_step": "Verify the checkout before reusing work.",
            "detail": "Earlier observations. " * 400,
        }))
        await db.update_project(task.project_id, status=ProjectStatus.PAUSED)
        if override:
            _write(str(tmp_path / ".aq" / "PRIME.md"),
                   "{{role}}\n{{task}}\n{{task_context}}\n{{task_context}}\n"
                   "{{messages}}\n{{workspaces}}\n{{tool_guidance}}\n{{completion_protocol}}")
        renderer = PrimeRenderer(db, config)
        before = await renderer.render_for_task(task.id, work_dir=str(tmp_path))
        # Use the explicit configured limit and make only inline policy large
        # enough to reproduce aggregate overflow from independently bounded prose.
        role = "Must preserve this exact policy. " + "r" * max(
            0, 34000 - len(before.to_markdown().encode("utf-8")),
        )
        _write(os.path.join(config.vault_agent_types, "coder", "profile.md"), "## Role\n" + role)
        before = await renderer.render_for_task(task.id, work_dir=str(tmp_path))
        budget = ContextBudget.from_config(config)
        assert budget.account(before.to_markdown())["diagnostic"] == "context.required_over_budget"
        config.knowledge.enabled = config.knowledge.context.enabled = config.memory.enabled = True
        config.knowledge.enabled_projects = [task.project_id]
        after = await renderer.render_for_task(task.id, work_dir=str(tmp_path))
        budget.enforce(after.to_markdown())
        assert role.strip() in after.to_markdown()
        assert "code" in next(s.body for s in after.sections if s.key == "task")
        assert "Keep the human acceptance gate." in after.to_markdown()
        assert "Verify the checkout before reusing work." in after.to_markdown()
        assert "--field context --offset 0 --limit 4096" in after.to_markdown()
        assert "Project paused" in after.to_markdown()
        assert "aq task close" in after.to_markdown()
        assert "--field prime_context" in after.to_markdown()
        assert "--field description" in after.to_markdown()
        full = await renderer.render_for_task(task.id, work_dir=str(tmp_path), bounded_worker=False)
        full_context = next(s.body for s in full.sections if s.key == "task_context")
        assert salvage in full_context and "Historical recovery snapshot" in full_context
        assert (await db.get_task(task.id)).description == description
        assert "FINAL ACCEPTANCE" in full.to_markdown()
        assert not budget.account(full.to_markdown())["fits"]

    async def test_handoff_paging_keeps_complete_comments_messages_and_current_facts(
        self, db, config, task, tmp_path, monkeypatch,
    ):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        from src.knowledge.budget import ContextBudget
        from src.prime.budgets import field_page

        # No ordinary context and a small description: only the historical
        # handoff can pay for this overflow. Comments and messages stay inline.
        comment = "Keep this complete human decision. αβ\n" * 60 + "FINAL DECISION"
        await db.add_task_comment(task.id, comment, author_kind="agent", author_id="worker")
        stored_note = json.dumps({"detail": "Prior observation αβ. " * 500 + "FINAL NOTE"})
        row_id = await db.add_task_context(task.id, type="handoff", label="handoff", content=stored_note)
        message = SimpleNamespace(id="msg-current", from_kind="session", from_id="supervisor",
                                  subject=None, body="Current instruction stays inline.",
                                  priority=0, created_at=1)
        config.messages.enabled = True
        monkeypatch.setattr(db, "get_pending_messages", AsyncMock(return_value=[message]))
        marked = AsyncMock()
        monkeypatch.setattr(db, "mark_delivered", marked)
        renderer = PrimeRenderer(db, config)
        before = await renderer.render_for_task(task.id, work_dir=str(tmp_path))
        before_message = next(s.body for s in before.sections if s.key == "messages")
        config.knowledge.enabled = config.knowledge.context.enabled = config.memory.enabled = True
        config.knowledge.enabled_projects = [task.project_id]
        config.knowledge.context.input_max_tokens = (
            ContextBudget.from_config(config).account(before.to_markdown())["total_tokens"] - 1200
        )
        after = await renderer.render_for_task(task.id, work_dir=str(tmp_path), mark_messages_delivered=True)
        ContextBudget.from_config(config).enforce(after.to_markdown())
        message_section = next(s.body for s in after.sections if s.key == "messages")
        assert len(message_section.encode()) < len(before_message.encode())
        assert "--field context --offset 0 --limit 4096" in message_section
        assert str(row_id) in message_section
        assert "Current instruction stays inline." in message_section
        assert "Current daemon facts (observed now)" in message_section
        assert "work_dir:" in message_section
        assert "Do the thing, carefully." in after.to_markdown()  # cheaper than a pointer
        assert "\n".join("> " + line for line in comment.split("\n")) in after.to_markdown()
        marked.assert_awaited_once_with(message.id, via="prime")
        # Follow the exact task-show paging implementation to recover the note.
        info = {"id": task.id, "context": await db.get_task_contexts(task.id)}
        chunks, offset = [], 0
        while True:
            page = field_page(info, "context", offset, 4096)
            chunks.append(page["content"])
            offset = page["next_offset"]
            if offset is None:
                break
        rows = json.loads("".join(chunks))
        assert next(r["content"] for r in rows if r["id"] == row_id) == stored_note

    async def test_unpageable_inline_policy_still_refuses_delivery(self, db, config, task):
        from src.knowledge.budget import ContextBudget
        from src.records.models import RecordError

        config.knowledge.context.input_max_tokens = 32768
        config.knowledge.enabled = config.knowledge.context.enabled = config.memory.enabled = True
        config.knowledge.enabled_projects = [task.project_id]
        role = "MANDATORY POLICY " * 3000
        _write(os.path.join(config.vault_agent_types, "coder", "profile.md"), "## Role\n" + role)
        await db.add_task_context(task.id, type="note", label="Context", content="c" * 20000)
        doc = await PrimeRenderer(db, config).render_for_task(task.id)
        assert role.strip() in doc.to_markdown()
        assert "--field prime_context" in doc.to_markdown()
        with pytest.raises(RecordError, match="context.required_over_budget"):
            ContextBudget.from_config(config).enforce(doc.to_markdown())

    async def test_preview_fitting_uses_the_observed_model_window(
        self, db, config, task, monkeypatch,
    ):
        from unittest.mock import AsyncMock
        from src.knowledge.budget import ContextBudget

        await db.add_task_context(task.id, type="note", label="Context", content="c" * 6000)
        renderer = PrimeRenderer(db, config)
        before = await renderer.render_for_task(task.id)
        window = ContextBudget.from_config(config).account(before.to_markdown())["total_tokens"] - 1000
        monkeypatch.setattr("src.sessions.context.read_context", AsyncMock(return_value={
            "model_context_window": window,
        }))
        config.knowledge.enabled = config.knowledge.context.enabled = config.memory.enabled = True
        config.knowledge.enabled_projects = [task.project_id]
        after = await renderer.render_for_task(task.id)
        ContextBudget.from_config(config, model_context_window=window).enforce(after.to_markdown())
        assert "--field prime_context" in after.to_markdown()
        assert len(after.to_markdown()) < len(before.to_markdown())
        assert "aq task close" in after.to_markdown()

    async def test_worker_previews_bound_description_and_context_but_keep_protocol(
        self, db, config, task
    ):
        from src.prime.budgets import CONTEXT_BYTES, DESCRIPTION_BYTES
        from src.sessions.worker_recovery import CONTINUATION_KEY

        description = "Requested work.\n" + "big brief " * 5000 + "EXACT ACCEPTANCE AT END"
        await db.update_task(task.id, description=description)
        await db.add_task_context(task.id, type="note", label="huge", content="context " * 10000)
        await db.set_task_meta(task.id, CONTINUATION_KEY, {"cause": "context_thrash",
            "count": 1, "progress": [{"body": "parser implemented; verify next"}]})
        doc = await PrimeRenderer(db, config).render_for_task(task.id)
        sections = {s.key: s.body for s in doc.sections}
        assert len(sections["task_context"].encode()) <= CONTEXT_BYTES
        assert "parser implemented" in sections["task_context"]
        assert "--field prime_context" in sections["task_context"]
        assert "--field description" in sections["task"]
        assert len(sections["task"].encode()) < DESCRIPTION_BYTES + 500
        assert "aq task close" in sections["completion_protocol"]
        assert (await db.get_task(task.id)).description == description
        full = await PrimeRenderer(db, config).render_for_task(task.id, bounded_worker=False)
        assert "EXACT ACCEPTANCE AT END" in full.to_markdown()

    async def test_oversized_spec_context_keeps_current_repair_authority(
        self, db, config, task, monkeypatch
    ):
        from dataclasses import replace
        from unittest.mock import AsyncMock
        from src.prime.sections import build_task_context_section
        from src.prime.budgets import CONTEXT_BYTES

        spec_rel = "projects/proj-1/specs/large.md"
        _write(os.path.join(config.vault_root, spec_rel), "## Contract\n" + "schema " * 10000)
        await db.add_task_context(task.id, type="spec_ref", label="contract",
                                  content=json.dumps({"path": spec_rel, "section": "Contract"}))
        snapshot = {"intent_id": "live-intent", "operation_id": "live-operation", "fence": 7}
        monkeypatch.setattr(db, "get_parent_repair_prime_context", AsyncMock(return_value=snapshot))
        section = await build_task_context_section(
            db, config, replace(task, created_by_kind="integration_repair"),
            session_id="worker", bounded_worker=True,
        )
        assert '"intent_id": "live-intent"' in section.body
        assert '"fence": 7' in section.body
        assert "--field prime_context" in section.body
        assert len(section.body.encode()) < CONTEXT_BYTES + 1000
        # Aggregate shrinking must keep the live authority outside the
        # pageable spec body, even when the excerpt becomes a pointer only.
        from datetime import datetime, timezone
        from src.knowledge.budget import ContextBudget
        from src.prime.renderer import _fit_worker_previews

        doc = PrimeDocument(task_id=task.id, session_id="worker", source="default",
                            rendered_at=datetime.now(timezone.utc), sections=(section,))
        budget = ContextBudget(input_tokens=1800, output_tokens=0, wrapper_tokens=0)
        fitted = _fit_worker_previews(doc, budget, None, None)
        budget.enforce(fitted.to_markdown())
        assert '"intent_id": "live-intent"' in fitted.to_markdown()
        assert '"operation_id": "live-operation"' in fitted.to_markdown()
        assert '"fence": 7' in fitted.to_markdown()
        assert "--field prime_context" in fitted.to_markdown()

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
    async def test_tool_guidance_mentions_cli_and_canonical_tools(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["tool_guidance"]
        assert "aq " in body
        for name in ("task_show", "task_set", "aq knowledge", "aq record", "aq-knowledge"):
            assert name in body

    async def test_tool_guidance_authorizes_local_commits_and_guards_publication(
        self, db, config, task
    ):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = " ".join({s.key: s.body for s in doc.sections}["tool_guidance"].split())
        for required in (
            "Commit with plain `git` in your own worktree",
            "`aq git commit` is a daemon-side command unavailable to worker scope",
            "`out of scope: git_commit` is expected",
            "A local `git commit` is authorized and is not a bypass",
            "For a first publication of a new task branch or a later fast-forward update",
            "`aq git push --expected-remote-oid <your-last-observed-remote-oid>`",
            "must name the remote OID you last observed for your own branch, never another",
            "never guess a lease or use plain `git push`",
            "Never bypass any other AQ rejection",
            "The daemon injects",
            "into task and pool worker sessions",
            "project override, else installation default, else",
            "`Agent Queue <agent-queue@localhost>` (`src/git/identity.py`)",
            "do not set an identity yourself or change Git config",
            "If they are absent, report the launch bug",
            "Never pass `--no-verify` or amend a pushed commit",
        ):
            assert required in body, required
        for variable in (
            "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"
        ):
            assert variable in body, variable

    async def test_tool_guidance_carries_test_scope_and_baseline_policy(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = " ".join({s.key: s.body for s in doc.sections}["tool_guidance"].lower().split())
        for required in (
            "aq test",
            "focused tests",
            "area suite",
            "full-suite runs belong to ci",
            "recorded known-failing baseline",
            "instead of capturing your own baseline",
            "pre-existing failure does not fail your task",
            "weaken or skip a test",
            'not "run the full suite before closing"',
        ):
            assert required in body, required

    @pytest.mark.parametrize("harness", ["opencode", "opencode-zen", "codex"])
    async def test_opencode_session_is_told_not_to_ask_optional_questions(
        self, db, config, task, harness
    ):
        # OpenCode's native question dialog blocks the whole session, and its
        # compaction prompt invites one (prime-dune): only OpenCode gets the
        # addendum, and only for the live session holding the task.  The
        # addendum is the CLI's: ``opencode-zen`` runs the same executable
        # against OpenCode Zen and gets it too (noble-delta-40).
        from src.sessions.harness_parser import Harness
        from src.sessions.harness_registry import HarnessRegistry

        registry = HarnessRegistry()
        registry.upsert(Harness(id="opencode-zen", command="/opt/harness-bin/opencode"))
        registry.upsert(Harness(id="codex", command="codex"))
        await db.create_session(
            SessionRecord(
                id="oc-session",
                project_id="proj-1",
                profile_id="coder",
                harness=harness,
                provider="fake",
                name="oc-session",
                lifecycle="pool",
                work_dir="/tmp/oc-session",
                epoch="test",
                instance_token="test-only",
                started_at=time.time(),
                task_id="task-1",
                state="running",
            )
        )
        doc = await PrimeRenderer(db, config, harness_registry=registry).render_for_task(
            "task-1", session_id="oc-session"
        )
        body = " ".join({s.key: s.body for s in doc.sections}["tool_guidance"].split())
        addendum = (
            "Do not ask optional questions",
            "should I continue/proceed",
            "Under AQ that means continue",
            "finish everything that does not depend on it first",
            "never work around it",
            "[aq question answered]",
        )
        if harness.startswith("opencode"):
            for required in addendum:
                assert required in body, required
            assert "aq test" in body  # appended to, never replacing, the shared guidance
        else:
            assert not any(required in body for required in addendum)

    async def test_completion_protocol_embeds_task_id(self, db, config, task):
        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        body = {s.key: s.body for s in doc.sections}["completion_protocol"]
        assert "aq task close task-1" in body
        assert "aq session drain-ack" in body
        assert "--deliverable-unmet 'id: reason'" in body

    @pytest.mark.parametrize("harness", ["claude", "codex"])
    @pytest.mark.parametrize("delivery_mode", ["disabled", "development"])
    async def test_worker_prime_uses_granted_daemon_delivery_commands(
        self, db, config, task, harness, delivery_mode
    ):
        profile_id = f"worker-{harness}"
        shipped = (
            Path(__file__).resolve().parents[1]
            / "src" / "profiles" / "defaults" / profile_id / "profile.md"
        ).read_text(encoding="utf-8")
        parsed = parse_profile(shipped)
        assert parsed.errors == []
        assert {"git_push", "git_create_pr"} <= set(parsed.capabilities["plugin_tools"])
        await db.update_profile(
            "coder",
            aq_commands=parsed.capabilities["aq_commands"],
            harness_tools=parsed.capabilities["harness_tools"],
            plugin_tools=parsed.capabilities["plugin_tools"],
        )
        _write(os.path.join(config.vault_agent_types, "coder", "profile.md"), shipped)
        await db.update_project("proj-1", hierarchical_integration_mode=delivery_mode)

        doc = await PrimeRenderer(db, config).render_for_task("task-1")
        by_key = {section.key: section.body for section in doc.sections}
        assert "generic coding worker" in by_key["role"]
        body = by_key["completion_protocol"]
        assert "aq git push" in body
        assert "aq git create-pr --title" in body
        assert "git push -u origin HEAD" not in body
        assert "gh pr create" not in body
        assert "aq doctor --check profiles.system_drift" in body
        assert "--grants-only" in body
        if delivery_mode == "development":
            assert "## Development delivery" in body
            assert "--expected-remote-oid" not in body
        else:
            assert "## Prepare feature history before review" in body
            assert "aq git push --expected-remote-oid <your-last-observed-remote-oid>" in body

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
        # A child of the held task is the server default (bright-falcon);
        # the guidance must not revive the retired sibling default.
        assert "child of the task you hold" in body
        assert "sibling" not in body
        assert "--parent <id>" in body
        assert "merely to make your close pass" in body
        assert "--root" in body
        assert "receives a routing gate" in body
        assert "--parent` and `--root` cannot be combined" in body
        # A finding nested under the held task blocks the close; the guidance
        # names the non-destructive repair rather than re-filing or abandoning.
        assert "aq task reparent --task-id <finding-id>" in body
        assert "hierarchy.open_children" in body

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



class TestToolGuidanceProjectScope:
    """The addendum follows the harness file that launches, in its project.

    A project may shadow a harness id with its own file, and the launch path
    reads that file (``orchestrator/execution.py`` resolves the harness with
    ``task.project_id``, as does ``orchestrator/pools.py``).  So when the
    shadow names another executable, the session launched in that project runs
    that CLI, and the addendum for the CLI it does *not* run must be withheld
    -- while every other project on the same harness id still gets it.
    """

    #: One phrase from OpenCode's addendum, absent from the shared guidance.
    ADDENDUM = "Do not ask optional questions"
    #: The hosted-pool recipe of ``noble-delta-40``: the ``opencode``
    #: executable against the OpenCode Zen gateway.
    ZEN = "/home/u/.agent-queue/harness-bin/opencode"

    @classmethod
    def _registry(cls, system_command, *, project_command=None):
        from src.sessions.harness_parser import Harness
        from src.sessions.harness_registry import HarnessRegistry

        registry = HarnessRegistry()
        registry.upsert(Harness(id="opencode-zen", command=system_command, provider="opencode"))
        if project_command is not None:
            registry.upsert(
                Harness(
                    id="opencode-zen",
                    command=project_command,
                    provider="opencode",
                    project_id="proj-1",
                )
            )
        return registry

    @staticmethod
    async def _second_project_task(db):
        await db.create_project(Project(id="proj-2", name="Other Project"))
        await db.create_task(
            Task(
                id="task-2",
                project_id="proj-2",
                title="Elsewhere",
                description="",
                profile_id="coder",
                route_source="legacy",
            )
        )

    @staticmethod
    async def _running_session(db, *, session_id, project_id, task_id, harness="opencode-zen"):
        await db.create_session(
            SessionRecord(
                id=session_id,
                project_id=project_id,
                profile_id="coder",
                harness=harness,
                provider="fake",
                name=session_id,
                lifecycle="pool",
                work_dir=f"/tmp/{session_id}",
                epoch="test",
                instance_token=f"token-{session_id}",
                started_at=time.time(),
                task_id=task_id,
                state="running",
            )
        )

    @staticmethod
    async def _guidance(db, config, registry, *, session_id, task_id):
        doc = await PrimeRenderer(db, config, harness_registry=registry).render_for_task(
            task_id, session_id=session_id
        )
        return " ".join({s.key: s.body for s in doc.sections}["tool_guidance"].split())

    async def test_a_project_override_of_the_command_withholds_the_addendum(
        self, db, config, task
    ):
        registry = self._registry(self.ZEN, project_command="claude")
        await self._second_project_task(db)
        await self._running_session(db, session_id="s-p1", project_id="proj-1", task_id="task-1")
        await self._running_session(db, session_id="s-p2", project_id="proj-2", task_id="task-2")

        overridden = await self._guidance(db, config, registry, session_id="s-p1", task_id="task-1")
        inherited = await self._guidance(db, config, registry, session_id="s-p2", task_id="task-2")

        assert self.ADDENDUM not in overridden
        assert "aq test" in overridden  # the shared guidance is withheld, not truncated
        assert self.ADDENDUM in inherited

    async def test_the_reverse_override_grants_the_addendum_to_one_project(self, db, config, task):
        # The system file runs another CLI; proj-1 overrides it back to
        # opencode, so the addendum is added there and nowhere else.
        registry = self._registry("/opt/bin/codex", project_command=self.ZEN)
        await self._second_project_task(db)
        await self._running_session(db, session_id="s-p1", project_id="proj-1", task_id="task-1")
        await self._running_session(db, session_id="s-p2", project_id="proj-2", task_id="task-2")

        granted = await self._guidance(db, config, registry, session_id="s-p1", task_id="task-1")
        inherited = await self._guidance(db, config, registry, session_id="s-p2", task_id="task-2")

        assert self.ADDENDUM in granted
        assert self.ADDENDUM not in inherited

    async def test_an_override_that_keeps_the_command_keeps_the_addendum(self, db, config, task):
        # The common override changes launch flags, not the executable, and a
        # Windows path spells the same binary: the addendum must survive.
        registry = self._registry(self.ZEN, project_command="C:/bin/opencode.EXE")
        await self._running_session(db, session_id="s-p1", project_id="proj-1", task_id="task-1")

        body = await self._guidance(db, config, registry, session_id="s-p1", task_id="task-1")
        assert self.ADDENDUM in body

    async def test_a_project_only_harness_id_is_offered_to_that_project_alone(
        self, db, config, task
    ):
        from src.sessions.harness_parser import Harness
        from src.sessions.harness_registry import HarnessRegistry

        # No system file at all: on proj-2 the harness does not resolve, so no
        # addendum may be attributed to a session there.
        registry = HarnessRegistry()
        registry.upsert(
            Harness(
                id="zen-local",
                command=self.ZEN,
                provider="opencode",
                project_id="proj-1",
            )
        )
        await self._second_project_task(db)
        await self._running_session(
            db, session_id="s-p1", project_id="proj-1", task_id="task-1", harness="zen-local"
        )
        await self._running_session(
            db, session_id="s-p2", project_id="proj-2", task_id="task-2", harness="zen-local"
        )

        granted = await self._guidance(db, config, registry, session_id="s-p1", task_id="task-1")
        elsewhere = await self._guidance(db, config, registry, session_id="s-p2", task_id="task-2")

        assert self.ADDENDUM in granted
        assert self.ADDENDUM not in elsewhere

    async def test_without_a_registry_the_id_match_still_answers(self, db, config, task):
        # Unchanged contract: no registry means id match only, so a project
        # scope cannot withhold the addendum from a harness named opencode.
        await self._running_session(
            db, session_id="s-oc", project_id="proj-1", task_id="task-1", harness="opencode"
        )

        body = await self._guidance(db, config, None, session_id="s-oc", task_id="task-1")
        assert self.ADDENDUM in body


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


# ---------------------------------------------------------------------------
# The static templates may only instruct commands the token can execute
# ---------------------------------------------------------------------------


#: ``aq <group> <sub>`` phrase -> the command name ``check_command_scope``
#: gates, or ``None`` for a CLI verb that never reaches the daemon.
_CLI_TO_COMMAND: dict[str, str | None] = {
    "aq decision record": "decision_record",
    "aq decision list": "decision_list",
    "aq test": None,
    # The ``[aq question answered]`` marker AQ itself types, not a command.
    "aq question answered": None,
    "aq git commit": "git_commit",
    "aq git push": "git_push",
    "aq git create-pr": "git_create_pr",
    "aq agent profile-reseed": "profile_reseed",
    "aq prime": "prime",
    "aq schema": "get_schema",
    "aq doctor": "doctor_run",
    "aq handoff": "task_handoff",
    "aq inbox": "message_inbox",
    "aq session": "session_list",
    "aq session drain-ack": "session_drain_ack",
    "aq task add-dependency": "add_dependency",
    "aq task claim": "task_claim",
    "aq task close": "task_close",
    "aq task comment": "task_comment",
    "aq task comments": "task_comments",
    "aq task create": "create_task",
    "aq task delete": "delete_task",
    "aq task heartbeat": "task_heartbeat",
    "aq task list": "list_tasks",
    "aq task reparent": "reparent_task",
    "aq task restart": "restart_task",
    "aq task set": "task_set",
    "aq task show": "task_show",
    "aq task subtask-done": "task_subtask_update",
    "aq task subtask-skip": "task_subtask_update",
    "aq task subtask-show": "task_subtask_get",
    "aq task subtasks": "task_subtasks",
    "aq message inbox": "message_inbox",
    "aq message reply": "message_reply",
    "aq message send": "message_send",
    "aq knowledge create": "knowledge_create",
    "aq knowledge show": "knowledge_show",
    "aq knowledge update": "knowledge_update",
    "aq record capabilities": "record_capabilities",
    "aq record search": "record_search",
    "aq memory save": "memory_save",
    "aq memory search": "memory_search",
    "aq project ready": "project_ready",
    "aq review submit": "review_submit",
    "aq job submit": "job_submit",
    "aq wait register": "wait_register",
    "aq wait show": "wait_get",
}


def _aq_phrases(paragraph: str) -> set[str]:
    """The ``aq …`` invocations a paragraph names, normalized across line wraps.

    Resolves each hit to the longest form that ``_CLI_TO_COMMAND`` knows, so
    "aq session drain-ack" is not read as the bare group "aq session" and
    "aq test tests/foo.py" is not read as a two-word subcommand.  An unknown
    hit keeps its longest form, which is what the caller reports.
    """
    import re

    flat = " ".join(paragraph.split())
    found = set()
    for match in re.finditer(r"\baq(?:\s+[a-z][a-z0-9-]*){1,2}", flat):
        words = match.group(0).split()
        for candidate in (" ".join(words), " ".join(words[:2])):
            if candidate in _CLI_TO_COMMAND:
                found.add(candidate)
                break
        else:
            found.add(" ".join(words))
    return found


class TestStaticGuidanceStaysOnTheAgentSurface:
    """A prime template must not tell an agent to run a command its token refuses.

    Every session token is gated on ``AGENT_COMMAND_SET`` (aq-surface §7.2), so
    guidance naming an operator command is an instruction the agent can only
    discover is impossible by trying it — which is exactly how the reviewer
    reject path and the "deduplicate with ``aq task list``" filing step were
    found to be dead letters. A command outside the agent surface may still be
    *named*, but only in a paragraph that says it is refused, so a reader is
    told the outcome instead of spending a turn on it.
    """

    async def test_every_templated_aq_command_is_reachable_or_marked_refused(self):
        from src.api.scope import AGENT_COMMAND_SET, _WORKER_GIT_COMMANDS
        from src.prime.sections import _TEMPLATES_DIR

        unknown: list[tuple[str, str]] = []
        unreachable: list[tuple[str, str]] = []
        for path in sorted(_TEMPLATES_DIR.rglob("*.md")):
            for paragraph in path.read_text(encoding="utf-8").split("\n\n"):
                for phrase in _aq_phrases(paragraph):
                    if phrase not in _CLI_TO_COMMAND:
                        unknown.append((path.name, phrase))
                        continue
                    command = _CLI_TO_COMMAND[phrase]
                    if command is None or command in AGENT_COMMAND_SET | _WORKER_GIT_COMMANDS:
                        continue
                    if "out of scope" not in paragraph:
                        unreachable.append((path.name, phrase))
        assert not unknown, (
            f"unclassified aq phrases in prime templates: {unknown}. "
            "Add them to _CLI_TO_COMMAND with the command name they dispatch."
        )
        assert not unreachable, (
            f"prime templates instruct commands a session token refuses: {unreachable}. "
            "Either drop the instruction or name the command only in a paragraph "
            "that says it is out of scope."
        )

    async def test_the_guard_catches_a_reintroduced_operator_instruction(self):
        """The scan is only worth having if this exact regression fails it."""
        from src.api.scope import AGENT_COMMAND_SET

        stale = "Before filing, deduplicate with `aq task list` and any dedup keys."
        assert _aq_phrases(stale) == {"aq task list"}
        assert _CLI_TO_COMMAND["aq task list"] not in AGENT_COMMAND_SET
        assert "out of scope" not in stale

    @pytest.mark.parametrize(
        ("phrase", "command"), [("aq doctor", "doctor_run"), ("aq git commit", "git_commit")]
    )
    async def test_a_refused_command_named_with_its_refusal_is_allowed(self, phrase, command):
        """Naming an operator command as a counterexample stays legal."""
        from src.api.scope import AGENT_COMMAND_SET, _WORKER_GIT_COMMANDS

        marked = f"Worker scope refuses `{phrase}` with `out of scope: {command}`."
        assert _aq_phrases(marked) == {phrase}
        assert _CLI_TO_COMMAND[phrase] == command
        assert command not in AGENT_COMMAND_SET | _WORKER_GIT_COMMANDS
        assert "out of scope" in marked


class TestWakeBudget:
    async def test_optional_wake_budget_keeps_full_role_ownership_and_close(self, db, config, task):
        from src.handoffs import WAKE_BYTES

        rules = "Retain all authorization rules. " * 1000
        _write(
            os.path.join(config.vault_agent_types, "coder", "profile.md"),
            "## Role\nCoder.\n\n## Rules\n" + rules,
        )
        await db.update_task(task.id, claim_epoch=4)
        await db.add_task_context(
            task.id,
            type="handoff",
            label="handoff",
            content=json.dumps(
                {
                    "agent": {
                        "next_step": "Run the selected tests",
                        "uncertainties": ["Race unknown"],
                        "completed": ["😀" * 10000],
                    },
                    "facts": {"claim_epoch": 3, "work_dir": "/old", "branch": "old"},
                }
            ),
        )
        doc = await PrimeRenderer(db, config).render_for_task(task.id)
        sections = {s.key: s.body for s in doc.sections}
        assert len(sections["messages"].encode("utf-8")) <= WAKE_BYTES
        assert "Run the selected tests" in sections["messages"]
        assert "Race unknown" in sections["messages"]
        assert "Stale handoff files/checkout" in sections["messages"]
        assert "claim_epoch: 4" in sections["messages"]
        assert rules.strip() in sections["role"]
        assert "Claim epoch: 4" in sections["task"]
        assert "aq task close" in sections["completion_protocol"]
        assert len(doc.to_markdown().encode("utf-8")) > WAKE_BYTES

    async def test_new_session_checkout_wins_over_stale_handoff_and_task_metadata(
        self,
        db,
        config,
        task,
        tmp_path,
    ):
        from src.handoffs import HANDOFF_BYTES

        await db.set_task_meta(task.id, "work_dir", "/old-reused-slot")
        current = str(tmp_path / "current-checkout")
        await db.create_session(
            SessionRecord(
                id="current-session",
                project_id=task.project_id,
                profile_id="coder",
                harness="codex",
                provider="openai",
                name="current-session",
                lifecycle="pool",
                work_dir=current,
                epoch="test",
                instance_token="test-only",
                started_at=time.time(),
                task_id=task.id,
                state="running",
            )
        )
        await db.add_task_context(
            task.id,
            type="handoff",
            label="handoff",
            content=json.dumps(
                {
                    "agent": {"files": ["old/file.py"], "next_step": "Inspect current checkout"},
                    "facts": {"task_id": task.id, "work_dir": "/old-reused-slot", "claim_epoch": 0},
                }
            ),
        )
        doc = await PrimeRenderer(db, config).render_for_task(task.id, session_id="current-session")
        sections = {s.key: s.body for s in doc.sections}
        assert current in sections["workspaces"]
        assert "current-session" in sections["messages"]
        assert "Stale handoff files/checkout" in sections["messages"]
        assert "Inspect current checkout" in sections["messages"]
        assert len(sections["messages"].encode("utf-8")) <= HANDOFF_BYTES + 2048 + 4


async def test_late_wait_result_reaches_next_prime_with_granted_pointer(db, config, task, monkeypatch):
    from src.prime.sections import build_messages_section

    wait_id = "36ec6080-a9df-4a3e-b752-08930702d558"
    monkeypatch.setattr("src.database.queries.message_queries._new_message_id",
                        lambda: f"wait:{wait_id}:result")
    msg = await db.create_message(
        project_id="proj-1", from_kind="system", from_id="agent-waits",
        to_kind="task", to_id=task.id, body_kind="wait_result",
        body=json.dumps({"wait_id": wait_id, "state": "cancelled", "digest": {"reason": "claim_ended"}}),
    )
    section = await build_messages_section(db, task.id, config=config, mark_delivered=True)
    assert f"aq wait show {wait_id} --consume --json" in section.body
    assert "claim_ended" in section.body
    assert (await db.get_message(msg.id)).via == "prime"
    assert (await build_messages_section(db, task.id, config=config)).body == ""


@pytest.mark.parametrize("lifecycle", ["task", "pool"])
@pytest.mark.parametrize("development", [False, True])
async def test_compact_protocol_preserves_shared_rules_and_role_specific_close(lifecycle, development):
    from src.prime.sections import build_completion_protocol_section, build_tool_guidance_section

    body = build_completion_protocol_section(
        "held-1", lifecycle=lifecycle, development=development,
    ).body
    assert body.count("aq task close held-1") == 1
    assert "aq session drain-ack" in body
    assert "aq task heartbeat held-1" in body
    assert "aq task comment held-1" in body
    assert "--expected-description" in body
    for rule in ("--deliverable-unmet", "--test", "--command", "human gates", "aq git push"):
        assert rule in body
    if lifecycle == "pool":
        for rule in ("--claim-next", ".aq/claim.json", "stale_claim", "daemon_unreachable",
                     "ambiguous close", "drain_requested/session_exhausted", "retry_after",
                     "report the blocker ONCE", "If preparation is retained, preserve it"):
            assert rule in body
    guidance = build_tool_guidance_section().body
    for rule in ("AGENTS.md/CLAUDE.md", "directory instructions", "SKILL.md", "catalogue",
                 "Profile Role/Rules", "--save-output PATH", "evidence paths"):
        assert rule in guidance


@pytest.mark.parametrize("harness, readable", [("claude", True), ("codex", True),
                                               ("opencode", False), ("gemini", False)])
def test_primed_context_guidance_tracks_what_the_harness_reports(harness, readable):
    """The prime prompt must not promise a reading the harness never emits."""
    from types import SimpleNamespace

    from src.config import AppConfig
    from src.prime.sections import build_tool_guidance_section

    config = AppConfig()
    session = SimpleNamespace(harness=harness)
    unmeasured = build_tool_guidance_section(
        harness, config=config, session=session).body
    assert ("120000 measured input tokens" in unmeasured) is readable
    assert ("160000-token native compact setting" in unmeasured) is readable
    assert ("no context metric AQ can read" in unmeasured) is not readable
    if readable:
        measured = build_tool_guidance_section(
            harness, config=config, session=session,
            observation={"input_tokens": 130000}).body
        assert "Latest measured request input: 130000 tokens." in measured
