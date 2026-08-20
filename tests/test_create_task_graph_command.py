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
from src.models import AgentProfile, Project, TaskStatus

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
            result = await handler._cmd_create_task_graph(
                {"project_id": "p1", "spec_path": path}
            )
            assert "outside the vault" in result["error"], path


class TestGraphSource:
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
        assert await db.get_dependencies(ids["queries"]) == {ids["schema"]}

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
