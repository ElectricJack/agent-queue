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
from tests.db_fixtures import lease_dsn

FIXTURES = Path(__file__).parent / "fixtures" / "task_graphs"


@pytest.fixture
async def setup(tmp_path):
    db = Database(lease_dsn("graph.db"))
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


def _subtask_graph() -> dict:
    return {
        "version": 1,
        "parent": {"title": "Epic"},
        "nodes": [
            {
                "key": "a",
                "title": "A",
                "acceptance": ["x"],
                "subtasks": [
                    "first",
                    {"title": "second", "context": "the brief for two"},
                    "third",
                ],
            },
            {"key": "b", "title": "B", "acceptance": ["x"]},
        ],
    }


class TestNodeSubtasks:
    """``subtasks:`` seeded by the graph creator, in write_plan's transaction."""

    async def test_rows_are_created_ordinal_ordered_with_titles_and_contexts(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _subtask_graph()}
        )
        assert "error" not in result
        node_id = next(n["task_id"] for n in result["nodes"] if n["key"] == "a")
        rows = await db.list_task_subtasks(node_id)
        assert [(r["ordinal"], r["title"]) for r in rows] == [
            (1, "first"),
            (2, "second"),
            (3, "third"),
        ]
        assert all(r["status"] == "pending" for r in rows)
        assert (await db.get_task_subtask(node_id, 2))["context"] == "the brief for two"
        other_id = next(n["task_id"] for n in result["nodes"] if n["key"] == "b")
        assert await db.list_task_subtasks(other_id) == []

    async def test_report_counts_subtasks_per_node(self, setup):
        handler, _db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _subtask_graph()}
        )
        assert {n["key"]: n["subtasks"] for n in result["nodes"]} == {"a": 3, "b": 0}

    async def test_dry_run_reports_the_count_and_writes_nothing(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _subtask_graph(), "dry_run": True}
        )
        assert {n["key"]: n["subtasks"] for n in result["nodes"]} == {"a": 3, "b": 0}
        assert await db.list_tasks(project_id="p1") == []
        async with db._engine.connect() as conn:
            from sqlalchemy import func, select

            from src.database.tables import task_subtasks

            assert await conn.scalar(select(func.count()).select_from(task_subtasks)) == 0

    async def test_a_failed_insert_leaves_zero_subtask_rows(self, setup, monkeypatch):
        """The single-transaction contract (§12) covers the checklist too."""
        handler, db, _vault = setup
        from src.task_graph import creator as creator_module

        real_insert = creator_module._insert_task
        calls = {"n": 0}

        async def failing(conn, row):
            calls["n"] += 1
            if calls["n"] > 2:  # container + first node succeed
                raise RuntimeError("boom")
            await real_insert(conn, row)

        monkeypatch.setattr(creator_module, "_insert_task", failing)
        with pytest.raises(RuntimeError):
            await handler._cmd_create_task_graph(
                {"project_id": "p1", "graph": _subtask_graph()}
            )
        assert await db.list_tasks(project_id="p1") == []
        async with db._engine.connect() as conn:
            from sqlalchemy import func, select

            from src.database.tables import task_subtasks

            assert await conn.scalar(select(func.count()).select_from(task_subtasks)) == 0

    async def test_provisional_ids_seed_the_reserved_id_not_the_placeholder(self, setup):
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
            {"project_id": "p1", "graph": _subtask_graph(), "parent_id": "epic"}
        )
        assert result["provisional"] is True
        node_id = next(n["task_id"] for n in result["nodes"] if n["key"] == "a")
        assert node_id == "epic.1"
        assert [r["title"] for r in await db.list_task_subtasks(node_id)] == [
            "first",
            "second",
            "third",
        ]
        assert await db.list_task_subtasks("epic.?") == []

    async def test_prime_renders_the_subtasks_block_for_a_graph_created_task(self, setup):
        handler, db, _vault = setup
        from src.prime.sections import build_task_subtasks_summary

        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _subtask_graph()}
        )
        node_id = next(n["task_id"] for n in result["nodes"] if n["key"] == "a")
        summary = await build_task_subtasks_summary(db, await db.get_task(node_id))
        assert summary.startswith("## Subtasks")
        assert "- [ ] 1. first" in summary
        assert "- [ ] 3. third" in summary
        assert "the brief for two" not in summary


class TestSubtasksUnreportable:
    """A profile whose policy cannot tick the checklist earns a warning, not a refusal."""

    @staticmethod
    def _graph() -> dict:
        doc = _subtask_graph()
        doc["nodes"][0]["profile"] = "coding"
        return doc

    async def test_warning_when_the_profile_cannot_update_subtasks(self, setup):
        handler, db, _vault = setup
        await db.update_profile("coding", aq_commands=["task_close"])
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": self._graph()}
        )
        assert "error" not in result
        warnings = [w for w in result["warnings"] if w["rule"] == "subtasks_unreportable"]
        assert len(warnings) == 1
        assert warnings[0]["severity"] == "warning"
        assert warnings[0]["node"] == "a"
        assert "aq agent profile-reseed --profile-id coding --grants-only" in warnings[0]["detail"]
        node_id = next(n["task_id"] for n in result["nodes"] if n["key"] == "a")
        assert len(await db.list_task_subtasks(node_id)) == 3

    async def test_no_warning_when_the_profile_grants_the_command(self, setup):
        handler, db, _vault = setup
        await db.update_profile("coding", aq_commands=["task_subtask_update"])
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": self._graph()}
        )
        assert [w for w in result["warnings"] if w["rule"] == "subtasks_unreportable"] == []

    async def test_no_warning_for_a_node_without_subtasks(self, setup):
        handler, db, _vault = setup
        await db.update_profile("coding", aq_commands=["task_close"])
        doc = _simple_graph()
        doc["nodes"][0]["profile"] = "coding"
        result = await handler._cmd_create_task_graph({"project_id": "p1", "graph": doc})
        assert [w for w in result["warnings"] if w["rule"] == "subtasks_unreportable"] == []


def _phased_graph() -> dict:
    return {
        "version": 1,
        "parent": {"title": "Epic"},
        "phases": [
            {"key": "schema", "title": "Phase 1 — schema", "label": "schema"},
            {"key": "engine", "title": "Phase 2 — engine"},
        ],
        "nodes": [
            {"key": "tables", "title": "Tables", "acceptance": ["x"], "phase": "schema"},
            {"key": "queries", "title": "Queries", "acceptance": ["x"], "phase": "schema"},
            {"key": "cascade", "title": "Cascade", "acceptance": ["x"], "phase": "engine"},
            {"key": "docs", "title": "Docs", "acceptance": ["x"]},
        ],
    }


class TestGraphPhases:
    """``phases:`` — containers, metadata, flags and gate edges in one write."""

    async def test_ids_are_two_level_for_phased_nodes_and_flat_for_the_rest(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _phased_graph()}
        )
        assert "error" not in result, result
        epic = result["parent_id"]
        phases = {p["key"]: p["task_id"] for p in result["phases"]}
        nodes = {n["key"]: n["task_id"] for n in result["nodes"]}

        assert phases == {"schema": f"{epic}.1", "engine": f"{epic}.2"}
        assert nodes == {
            "tables": f"{epic}.1.1",
            "queries": f"{epic}.1.2",
            "cascade": f"{epic}.2.1",
            # Unphased nodes are numbered after the phases.
            "docs": f"{epic}.3",
        }
        assert (await db.get_task(nodes["tables"])).parent_task_id == phases["schema"]
        assert (await db.get_task(nodes["docs"])).parent_task_id == epic
        assert (await db.get_task(phases["schema"])).parent_task_id == epic

    async def test_each_phase_is_a_flagged_container_carrying_its_metadata(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _phased_graph()}
        )
        phases = {p["key"]: p["task_id"] for p in result["phases"]}

        assert await db.get_task_meta(phases["schema"], "container") is True
        assert await db.get_task_meta(phases["schema"], "phase") == {
            "order": 1,
            "label": "schema",
        }
        # No ``label:`` declared — the title stands in, as ``phase_create`` does.
        assert await db.get_task_meta(phases["engine"], "phase") == {
            "order": 2,
            "label": "Phase 2 — engine",
        }
        assert [p["order"] for p in result["phases"]] == [1, 2]

    async def test_a_phase_blocks_on_every_earlier_phase_not_just_the_previous(self, setup):
        """Three phases: deleting an abandoned middle one must not release the last."""
        handler, db, _vault = setup
        doc = _phased_graph()
        doc["phases"].append({"key": "docs-phase", "title": "Phase 3"})
        doc["nodes"][3]["phase"] = "docs-phase"
        result = await handler._cmd_create_task_graph({"project_id": "p1", "graph": doc})
        phases = {p["key"]: p["task_id"] for p in result["phases"]}

        assert await _blocks(db, phases["schema"]) == set()
        assert await _blocks(db, phases["engine"]) == {phases["schema"]}
        assert await _blocks(db, phases["docs-phase"]) == {
            phases["schema"],
            phases["engine"],
        }

    async def test_a_later_phase_is_projected_blocked(self, setup):
        """``recompute_blocked`` has to cover the phases, or phase 2 is claimable."""
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _phased_graph()}
        )
        phases = {p["key"]: p["task_id"] for p in result["phases"]}
        assert (await db.get_task(phases["schema"])).is_blocked is False
        assert (await db.get_task(phases["engine"])).is_blocked is True
        assert (await db.get_task(phases["engine"])).status == TaskStatus.DEFINED

    async def test_a_phase_with_no_nodes_is_still_a_flagged_container(self, setup):
        """The step-6 loop runs for every phase, not only those that got children."""
        handler, db, _vault = setup
        doc = _phased_graph()
        doc["phases"].append({"key": "empty", "title": "Phase 3 — empty"})
        result = await handler._cmd_create_task_graph({"project_id": "p1", "graph": doc})
        assert "error" not in result, result
        assert {w["rule"] for w in result["warnings"]} == {"phase_without_nodes"}

        empty = next(p["task_id"] for p in result["phases"] if p["key"] == "empty")
        assert await db.get_task_meta(empty, "container") is True
        assert await db.get_task_meta(empty, "phase") == {
            "order": 3,
            "label": "Phase 3 — empty",
        }
        # ...and therefore never claimable, even once something releases it.
        await db.transition_task(empty, TaskStatus.READY, force=True)
        assert empty not in await _frontier(db)

    async def test_a_failed_insert_leaves_no_phase_no_task_and_no_edge(self, setup, monkeypatch):
        handler, db, _vault = setup
        from src.task_graph import creator as creator_module

        real_insert = creator_module._insert_task
        calls = {"n": 0}

        async def failing(conn, row):
            calls["n"] += 1
            if calls["n"] > 4:  # container + two phases + first node succeed
                raise RuntimeError("boom")
            await real_insert(conn, row)

        monkeypatch.setattr(creator_module, "_insert_task", failing)
        with pytest.raises(RuntimeError):
            await handler._cmd_create_task_graph(
                {"project_id": "p1", "graph": _phased_graph()}
            )

        assert await db.list_tasks(project_id="p1") == []
        async with db._engine.connect() as conn:
            from sqlalchemy import func, select

            from src.database.tables import task_dependencies, task_metadata, task_subtasks

            for table in (task_dependencies, task_metadata, task_subtasks):
                assert await conn.scalar(select(func.count()).select_from(table)) == 0

    async def test_phases_under_an_existing_parent_are_refused(self, setup):
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
            {"project_id": "p1", "graph": _phased_graph(), "parent_id": "epic"}
        )
        assert result["code"] == "graph.phases_need_root"
        assert result["success"] is False
        assert await db.list_tasks(project_id="p1") == [await db.get_task("epic")]

    async def test_dry_run_reports_the_phases_and_writes_nothing(self, setup):
        handler, db, _vault = setup
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _phased_graph(), "dry_run": True}
        )
        epic = result["parent_id"]
        assert [(p["key"], p["task_id"], p["order"]) for p in result["phases"]] == [
            ("schema", f"{epic}.1", 1),
            ("engine", f"{epic}.2", 2),
        ]
        assert await db.list_tasks(project_id="p1") == []

    @pytest.mark.parametrize("mode", [None, "disabled", "observe"])
    async def test_a_phased_graph_is_created_in_every_non_hierarchical_mode(self, setup, mode):
        """§3.0's table is real: only hierarchy/train refuse."""
        handler, db, _vault = setup
        if mode is not None:
            await db.update_project("p1", hierarchical_integration_mode=mode)
        result = await handler._cmd_create_task_graph(
            {"project_id": "p1", "graph": _phased_graph()}
        )
        assert "error" not in result, result
        assert len(result["phases"]) == 2
        assert len(result["task_ids"]) == 4

    async def test_a_phased_node_may_also_carry_subtasks(self, setup):
        handler, db, _vault = setup
        doc = _phased_graph()
        doc["nodes"][0]["subtasks"] = ["first", "second"]
        result = await handler._cmd_create_task_graph({"project_id": "p1", "graph": doc})
        node_id = next(n["task_id"] for n in result["nodes"] if n["key"] == "tables")
        assert [r["title"] for r in await db.list_task_subtasks(node_id)] == ["first", "second"]
        assert (await db.get_task(node_id)).parent_task_id.endswith(".1")


async def _blocks(db, task_id) -> set[str]:
    from sqlalchemy import select

    from src.database.tables import task_dependencies

    async with db._engine.connect() as conn:
        rows = await conn.execute(
            select(task_dependencies.c.depends_on_task_id).where(
                task_dependencies.c.task_id == task_id,
                task_dependencies.c.dep_type == "blocks",
            )
        )
    return set(rows.scalars().all())


async def _frontier(db) -> set[str]:
    from sqlalchemy import select

    from src.database.queries.claim_queries import _frontier_where
    from src.database.tables import tasks

    async with db._engine.connect() as conn:
        rows = await conn.execute(select(tasks.c.id).where(_frontier_where("p1", None)))
    return set(rows.scalars().all())


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
