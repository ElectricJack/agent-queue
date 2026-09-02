"""``_cmd_create_task_graph`` — supervisor-agent §6.1 / §8 / §12.

Exercises the command surface end-to-end against a real SQLite db and a real
vault directory: parse → validate → create in one transaction, with
``--dry-run`` and the validation-error envelope.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.database import Database
from src.models import AgentProfile, Project, Task, TaskStatus

FIXTURES = Path(__file__).parent / "fixtures" / "task_graphs"


@pytest.fixture
async def setup(tmp_path):
    db = Database(str(tmp_path / "graph.db"))
    await db.initialize()
    await db.create_project(Project(id="p1", name="test"))
    for profile_id in ("coding", "planner"):
        await db.create_profile(AgentProfile(id=profile_id, name=profile_id))

    vault_root = tmp_path / "vault"
    specs = vault_root / "projects" / "p1" / "specs"
    specs.mkdir(parents=True)
    shutil.copy(FIXTURES / "valid_spec.md", specs / "messages-table.md")
    shutil.copy(FIXTURES / "missing_spec_section.md", specs / "partial.md")

    orch = MagicMock()
    orch.db = db
    orch._emit_notify = AsyncMock()

    config = MagicMock()
    config.vault_root = str(vault_root)

    handler = CommandHandler(orch, config)
    handler._active_project_id = None
    yield handler, db, str(vault_root)
    await db.close()


def _graph_doc() -> dict:
    return json.loads((FIXTURES / "valid.json").read_text(encoding="utf-8"))


class TestArgumentHandling:
    async def test_requires_exactly_one_source(self, setup):
        handler, _db, _vault = setup
        both = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _graph_doc(), "spec_path": "x.md"}
        )
        neither = await handler._cmd_create_task_graph({"project_id": "p1"})
        assert "exactly one of" in both["error"]
        assert "exactly one of" in neither["error"]

    async def test_unknown_project(self, setup):
        handler, _db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "ghost", "graph": _graph_doc()}
        )
        assert "not found" in result["error"]

    async def test_missing_project_without_active(self, setup):
        handler, _db, _vault = setup
        result = await handler._cmd_create_task_graph({"graph": _graph_doc()})
        assert "project_id is required" in result["error"]

    async def test_missing_spec_file(self, setup):
        handler, _db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "spec_path": "projects/p1/specs/nope.md"}
        )
        assert "not found in the vault" in result["error"]

    async def test_spec_path_escaping_the_vault_is_refused(self, setup, tmp_path):
        """`spec_path` accepted absolute paths and joined relative ones without
        normalisation, so `--from-spec` was an arbitrary file read."""
        handler, _db, _vault = setup
        outside = tmp_path / "secret.md"
        outside.write_text("# Secret\n\n```aq-graph\n{}\n```\n", encoding="utf-8")

        for path in ("../secret.md", str(outside)):
            result = await handler._cmd_create_task_graph({"project_id": "p1", "spec_path": path})
            assert "outside the vault" in result["error"], path


def _simple_graph() -> dict:
    return {
        "version": 1,
        "parent": {"title": "Epic"},
        "nodes": [
            {"key": "a", "title": "A", "acceptance": ["x"]},
            {"key": "b", "title": "B", "acceptance": ["x"], "needs": [{"on": "a"}]},
        ],
    }


class TestParentValidation:
    async def test_parent_not_found(self, setup):
        handler, _db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _simple_graph(), "parent_id": "ghost"}
        )
        assert result["code"] == "hierarchy.not_found"

    async def test_parent_in_another_project(self, setup):
        handler, db, _vault = setup
        await db.create_project(Project(id="p2", name="p2"))
        await db.create_task(Task(id="other-epic", project_id="p2", title="e", description="e"))
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _simple_graph(), "parent_id": "other-epic"}
        )
        assert result["code"] == "hierarchy.cross_project"

    async def test_parent_completed(self, setup):
        handler, db, _vault = setup
        await db.create_task(
            Task(
                id="done-epic",
                project_id="p1",
                title="e",
                description="e",
                status=TaskStatus.COMPLETED,
            )
        )
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _simple_graph(), "parent_id": "done-epic"}
        )
        assert result["code"] == "hierarchy.container_closed"

    async def test_parent_over_structural_depth_cap(self, setup):
        handler, db, _vault = setup
        # Build a chain root -> mid -> leaf, structural depth 3 (the cap).
        for tid in ("root", "mid", "leaf"):
            await db.create_task(Task(id=tid, project_id="p1", title=tid, description=tid))
        async with db._engine.begin() as conn:
            await db.set_parent("mid", "root", conn=conn)
            await db.set_parent("leaf", "mid", conn=conn)
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _simple_graph(), "parent_id": "leaf"}
        )
        assert result["code"] == "hierarchy.depth"

    async def test_parent_at_naming_depth_cap_is_refused_even_at_root(self, setup):
        """A task named ``a.1.1`` reparented to root has structural depth 1 and
        would pass the structural check, but its *naming* depth is already at
        the cap — minting ``a.1.1.1`` would exceed MAX_NAMING_DEPTH (spec §6:
        a graph cannot fall back to per-node root ids)."""
        handler, db, _vault = setup
        await db.create_task(Task(id="a.1.1", project_id="p1", title="e", description="e"))
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _simple_graph(), "parent_id": "a.1.1"}
        )
        assert result["code"] == "hierarchy.depth"
        assert await db.list_tasks(project_id="p1") == [await db.get_task("a.1.1")]

    async def test_creates_under_existing_parent_with_dotted_ids(self, setup):
        handler, db, _vault = setup
        await db.create_task(
            Task(
                id="epic",
                project_id="p1",
                title="e",
                description="e",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _simple_graph(), "parent_id": "epic"}
        )
        assert "error" not in result
        assert result["provisional"] is True
        assert result["task_ids"] == ["epic.1", "epic.2"]
        assert (await db.get_task("epic.1")).parent_task_id == "epic"

    async def test_dry_run_under_existing_parent_is_provisional_and_reserves_nothing(self, setup):
        handler, db, _vault = setup
        await db.create_task(
            Task(
                id="epic",
                project_id="p1",
                title="e",
                description="e",
                status=TaskStatus.IN_PROGRESS,
            )
        )
        result = await handler._cmd_create_task_graph(
            {
                "project_id": "p1",
                "graph": _simple_graph(),
                "parent_id": "epic",
                "dry_run": True,
            }
        )
        assert result["provisional"] is True
        assert result["task_ids"] == ["epic.?", "epic.?"]
        assert await db.get_task("epic.1") is None


class TestGraphSource:
    async def test_inline_graph_preserves_node_deliverables(self, setup):
        handler, db, _vault = setup
        graph = {
            "version": 1,
            "parent": {"title": "Epic"},
            "nodes": [{
                "key": "worker",
                "title": "Worker",
                "deliverables": [
                    {"id": "module", "kind": "file", "target": "src/new_module.py"}
                ],
            }],
        }
        result = await handler._cmd_create_task_graph({"project_id": "p1", "graph": graph})

        assert "error" not in result
        assert (await db.get_task(result["task_ids"][0])).deliverables == [
            {"id": "module", "kind": "file", "target": "src/new_module.py"}
        ]

    async def test_inline_graph_creates_everything(self, setup):
        handler, db, vault = setup
        doc = _graph_doc()
        doc["spec"] = str(Path(vault) / "projects" / "p1" / "specs" / "messages-table.md")
        result = await handler._cmd_create_task_graph({"project_id": "p1", "graph": doc})
        assert "error" not in result
        assert len(result["task_ids"]) == 3
        assert result["created"] is True
        assert result["project_id"] == "p1"
        assert (await db.get_task(result["parent_id"])).status == TaskStatus.IN_PROGRESS

    async def test_no_success_key_injected(self, setup):
        handler, _db, vault = setup
        doc = _graph_doc()
        doc["spec"] = str(Path(vault) / "projects" / "p1" / "specs" / "messages-table.md")
        result = await handler._cmd_create_task_graph({"project_id": "p1", "graph": doc})
        assert "success" not in result

    async def test_from_spec_vault_relative_path(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "spec_path": "projects/p1/specs/messages-table.md"}
        )
        assert "error" not in result
        assert len(result["task_ids"]) == 2
        assert result["spec"].endswith("messages-table.md")
        ids = {n["key"]: n["task_id"] for n in result["nodes"]}
        # Plus the ``parent-child`` edge ``set_parent`` writes for every node.
        assert await db.get_dependencies(ids["queries"]) == {ids["schema"], result["parent_id"]}

    async def test_from_spec_records_spec_ref_context(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "spec_path": "projects/p1/specs/messages-table.md"}
        )
        schema_id = next(n["task_id"] for n in result["nodes"] if n["key"] == "schema")
        contexts = await db.get_task_contexts(schema_id)
        spec_ref = next(c for c in contexts if c["type"] == "spec_ref")
        payload = json.loads(spec_ref["content"])
        assert payload["section"] == "3. Schema"
        assert payload["path"].endswith("messages-table.md")


class TestValidationEnvelope:
    async def test_errors_block_creation(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {
                "project_id": "p1",
                "graph": {
                    "version": 1,
                    "nodes": [
                        {"key": "a", "title": "A", "needs": ["b"]},
                        {"key": "b", "title": "B", "needs": ["a"]},
                    ],
                },
            }
        )
        assert "nothing was created" in result["error"]
        assert {e["rule"] for e in result["errors"]} == {"cycle"}
        assert await db.list_tasks(project_id="p1") == []

    async def test_missing_spec_section_is_an_error_from_a_spec(self, setup):
        handler, _db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "spec_path": "projects/p1/specs/partial.md"}
        )
        assert {e["rule"] for e in result["errors"]} == {"missing_spec_section"}

    async def test_warnings_are_reported_but_do_not_block(self, setup):
        handler, _db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {
                "project_id": "p1",
                "graph": {"version": 1, "nodes": [{"key": "a", "title": "A"}]},
            }
        )
        assert "error" not in result
        assert {w["rule"] for w in result["warnings"]} == {"no_acceptance"}

    async def test_parse_errors_are_structured(self, setup):
        handler, _db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": {"version": 1, "nodes": [{"title": "no key"}]}}
        )
        assert result["error"] == "graph document is invalid"
        assert result["errors"][0]["rule"] == "missing_key"


class TestDryRun:
    async def test_reports_ids_without_writing(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {
                "project_id": "p1",
                "spec_path": "projects/p1/specs/messages-table.md",
                "dry_run": True,
            }
        )
        assert result["dry_run"] is True
        assert result["created"] is False
        assert len(result["task_ids"]) == 2
        assert await db.list_tasks(project_id="p1") == []

    async def test_dry_run_output_is_stable_in_shape(self, setup):
        """Same keys as a real run — a dry run shows what a real run does."""
        handler, _db, _vault = setup
        args = {"project_id": "p1", "spec_path": "projects/p1/specs/messages-table.md"}
        dry = await handler._cmd_create_task_graph({**args, "dry_run": True})
        real = await handler._cmd_create_task_graph(args)
        assert set(dry) == set(real)
        assert [n["key"] for n in dry["nodes"]] == [n["key"] for n in real["nodes"]]
