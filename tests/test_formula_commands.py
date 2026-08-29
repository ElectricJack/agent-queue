"""formula_list / formula_show / formula_cook (spec §13, §14)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.database import Database
from src.models import AgentProfile, Project, TaskStatus
from src.task_graph.formulas import FormulaRegistry, load_from_vault

FIXTURES = Path(__file__).parent / "fixtures" / "formulas"


@pytest.fixture
async def setup(tmp_path):
    db = Database(str(tmp_path / "f.db"))
    await db.initialize()
    await db.create_project(Project(id="p1", name="test"))
    await db.create_project(Project(id="p2", name="other"))
    for pid in ("coding", "reviewer"):
        await db.create_profile(AgentProfile(id=pid, name=pid))
    vault_root = tmp_path / "vault"
    (vault_root / "formulas").mkdir(parents=True)
    (vault_root / "projects" / "p1" / "formulas").mkdir(parents=True)
    for name in ("base-review.md", "review-and-fix.md"):
        shutil.copy(FIXTURES / name, vault_root / "formulas" / name)
    registry = FormulaRegistry()
    assert load_from_vault(registry, str(vault_root)) == []
    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()
    orch.bus.emit = AsyncMock()
    orch.formula_registry = registry
    config = MagicMock()
    config.vault_root = str(vault_root)
    handler = CommandHandler(orch, config)
    handler._active_project_id = None
    yield handler, db, vault_root, registry
    await db.close()


class TestList:
    async def test_lists_with_scope_and_vars(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_list({"project_id": "p1"})
        names = {f["name"]: f for f in res["formulas"]}
        assert set(names) == {"base-review", "review-and-fix"}
        assert names["review-and-fix"]["extends"] == "base-review"
        assert names["base-review"]["vars"]["branch"] == {"required": True, "default": None, "enum": None}
        assert names["base-review"]["scope"] == "system"

    async def test_session_scope_pins_project(self, setup):
        h, *_ = setup
        h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": False}
        res = await h._cmd_formula_list({"project_id": "other"})
        assert res["success"] and all(f["scope"] in ("system", "project:p1") for f in res["formulas"])


class TestShow:
    async def test_show_resolves_and_substitutes(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_show({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "feat/x"}})
        assert res["success"] is True
        assert res["chain"] == ["base-review", "review-and-fix"]
        assert res["vars"]["effective"] == {"branch": "feat/x", "reviewer": "reviewer", "fixer": "coding"}
        titles = {n["key"]: n["title"] for n in res["graph"]["nodes"]}
        assert titles == {"review": "Review branch feat/x (strict)", "fix": "Fix findings on feat/x"}
        assert res["errors"] == []

    async def test_show_reports_missing_required_var(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_show({"name": "review-and-fix", "project_id": "p1"})
        assert res["success"] is False
        assert [e["rule"] for e in res["errors"]] == ["formula.var_required"]

    async def test_show_unknown_formula(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_show({"name": "nope", "project_id": "p1"})
        assert res["success"] is False and "nope" in res["error"]

    async def test_show_vars_must_be_a_mapping(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_show({"name": "base-review", "project_id": "p1", "vars": "nope"})
        assert res == {"success": False, "error": "vars must be an object of string values"}

    async def test_show_writes_nothing(self, setup):
        h, db, *_ = setup
        cooked = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                            "vars": {"branch": "b"}})
        cid = cooked["container_id"]
        task_count_before = len(await db.list_tasks("p1"))
        context_count_before = len(await db.get_task_contexts(cid))
        await h._cmd_formula_show({"name": "review-and-fix", "project_id": "p1",
                                   "vars": {"branch": "feat/x"}})
        assert len(await db.list_tasks("p1")) == task_count_before
        assert len(await db.get_task_contexts(cid)) == context_count_before


class TestCook:
    async def test_cook_creates_graph_with_provenance_and_event(self, setup):
        h, db, vault_root, _ = setup
        res = await h._cmd_formula_cook({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "feat/x", "fixer": "coding"}})
        assert res["success"] is True
        cid = res["container_id"]
        assert (await db.get_task(cid)).status == TaskStatus.IN_PROGRESS
        assert await db.get_task_meta(cid, "formula") == "review-and-fix"
        assert await db.get_task_meta(cid, "formula_path") == "formulas/review-and-fix.md"
        assert json.loads(await db.get_task_meta(cid, "formula_vars"))["branch"] == "feat/x"
        assert "formula:review-and-fix" in await db.get_task_labels(cid)
        kids = await db.get_children(cid)
        assert {k.title for k in kids} == {"Review branch feat/x (strict)", "Fix findings on feat/x"}
        fix = next(k for k in kids if k.title.startswith("Fix"))
        assert fix.profile_id == "coding" and fix.is_blocked is True
        emitted = [c.args for c in h.orchestrator.bus.emit.await_args_list if c.args[0] == "formula.cooked"]
        assert emitted and emitted[0][1]["container_id"] == cid

    async def test_cook_dry_run_writes_nothing(self, setup):
        h, db, *_ = setup
        res = await h._cmd_formula_cook({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "b"}, "dry_run": True})
        assert res["success"] and res["dry_run"] is True and res["provenance"]["name"] == "review-and-fix"
        assert await db.list_tasks("p1") == []

    async def test_cook_var_errors_block(self, setup):
        h, db, *_ = setup
        res = await h._cmd_formula_cook({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "b", "reviewer": "nobody"}})
        assert res["success"] is False and res["errors"][0]["rule"] == "formula.var_enum"
        assert await db.list_tasks("p1") == []

    async def test_cook_under_parent(self, setup):
        h, db, *_ = setup
        first = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                           "vars": {"branch": "b"}})
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                         "vars": {"branch": "c"}, "parent_id": first["container_id"]})
        assert res["success"] is True and res["container_id"] == first["container_id"]
        assert len(await db.get_children(first["container_id"])) == 2

    async def test_session_scope_refused(self, setup):
        h, *_ = setup
        h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": False}
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1", "vars": {"branch": "b"}})
        assert res["success"] is False

    async def test_elevated_session_scope_allowed(self, setup):
        h, *_ = setup
        h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": True}
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1", "vars": {"branch": "b"}})
        assert res["success"] is True

    async def test_cook_project_not_found(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "ghost",
                                         "vars": {"branch": "b"}})
        assert res == {"success": False, "error": "Project 'ghost' not found"}

    async def test_cook_vars_must_be_a_mapping(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1", "vars": "nope"})
        assert res == {"success": False, "error": "vars must be an object of string values"}

    async def test_cook_cross_project_parent_rejected(self, setup):
        h, *_ = setup
        other = await h._cmd_formula_cook({"name": "base-review", "project_id": "p2",
                                           "vars": {"branch": "b"}})
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                         "vars": {"branch": "c"}, "parent_id": other["container_id"]})
        assert res["success"] is False and res["code"] == "hierarchy.cross_project"

    async def test_cook_report_has_project_id(self, setup):
        h, *_ = setup
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                         "vars": {"branch": "b"}})
        assert res["project_id"] == "p1"


class TestAsCooked:
    async def test_as_cooked_tolerates_legacy_bare_document_row(self, setup):
        h, db, *_ = setup
        res = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                         "vars": {"branch": "b"}})
        cid = res["container_id"]
        # A pre-envelope row: the bare document with no cooked_at/chain_sha.
        await db.add_task_context(
            cid, type="formula_snapshot", label="base-review",
            content=json.dumps({"version": 1, "nodes": [{"key": "legacy", "title": "L"}]}),
        )
        shown = await h._cmd_formula_show({"as_cooked": cid})
        assert shown["success"] is True
        assert {n["key"] for n in shown["graph"]["nodes"]} in ({"legacy"}, {"review"})

    async def test_as_cooked_renders_snapshot_not_current_file(self, setup):
        h, db, vault_root, registry = setup
        res = await h._cmd_formula_cook({"name": "review-and-fix", "project_id": "p1",
                                         "vars": {"branch": "feat/x"}})
        cid = res["container_id"]
        path = vault_root / "formulas" / "review-and-fix.md"
        path.write_text(path.read_text().replace("Fix findings on {branch}", "CHANGED {branch}"))
        load_from_vault(registry, str(vault_root))
        shown = await h._cmd_formula_show({"as_cooked": cid})
        assert shown["success"] and shown["as_cooked"] == cid
        titles = {n["key"]: n["title"] for n in shown["graph"]["nodes"]}
        assert titles["fix"] == "Fix findings on feat/x"
        assert shown["chain_sha"] == await db.get_task_meta(cid, "formula_chain_sha")
        assert await h._cmd_formula_show({"as_cooked": "nope"}) == {
            "success": False, "error": "no formula snapshot on nope"}

    async def test_as_cooked_renders_the_second_cook(self, setup):
        h, db, *_ = setup
        first = await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                           "vars": {"branch": "b"}})
        cid = first["container_id"]
        await asyncio.sleep(0.01)
        await h._cmd_formula_cook({"name": "base-review", "project_id": "p1",
                                   "vars": {"branch": "second"}, "parent_id": cid})
        shown = await h._cmd_formula_show({"as_cooked": cid})
        titles = {n["key"]: n["title"] for n in shown["graph"]["nodes"]}
        assert titles["review"] == "Review second"

    async def test_as_cooked_out_of_scope_for_other_project(self, setup):
        h, *_ = setup
        other = await h._cmd_formula_cook({"name": "base-review", "project_id": "p2",
                                           "vars": {"branch": "b"}})
        h._current_scope = {"kind": "session", "session_id": "s", "project_id": "p1", "elevated": False}
        res = await h._cmd_formula_show({"as_cooked": other["container_id"]})
        assert res["success"] is False and res["result"] == "out_of_scope"
