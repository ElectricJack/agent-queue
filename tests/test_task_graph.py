"""Task-graph parser/validator/creator — supervisor-agent §8 and §12.

The validator is exercised against golden files in
``tests/fixtures/task_graphs``: one input document per §8.3 rule, one golden
list of expected findings.  Rule names are part of the contract, so a golden
mismatch is a real API change, not test churn.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.task_graph import (
    GraphParseError,
    create_graph,
    extract_graph_block,
    extract_graph_from_spec,
    parse_graph,
    split_findings,
    substitute_vars,
    validate_graph,
)
from src.database.queries.task_subtask_queries import (
    MAX_SUBTASK_CONTEXT,
    MAX_SUBTASK_TITLE,
    MAX_SUBTASKS_PER_CALL,
)
from src.task_graph.creator import build_plan, write_plan
from src.task_graph.models import TaskGraph
from tests.db_fixtures import lease_dsn

FIXTURES = Path(__file__).parent / "fixtures" / "task_graphs"
GOLDEN = FIXTURES / "golden"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTask:
    def __init__(self, task_id: str, project_id: str):
        self.id = task_id
        self.project_id = project_id


class _FakeProfile:
    def __init__(self, profile_id: str):
        self.id = profile_id


class _FakeDB:
    """Minimal db surface the validator uses: get_task + get_profile."""

    def __init__(self, tasks: dict[str, str] | None = None, profiles: set[str] | None = None):
        self._tasks = tasks or {"foreign-task": "other-project", "local-task": "p1"}
        self._profiles = profiles if profiles is not None else {"coding", "planner", "reviewer"}

    async def get_task(self, task_id: str):
        project = self._tasks.get(task_id)
        return _FakeTask(task_id, project) if project else None

    async def get_profile(self, profile_id: str):
        return _FakeProfile(profile_id) if profile_id in self._profiles else None


@pytest.fixture
def vault(tmp_path):
    """A tmp vault with the fixture specs installed under projects/p1/specs."""
    specs = tmp_path / "vault" / "projects" / "p1" / "specs"
    specs.mkdir(parents=True)
    shutil.copy(FIXTURES / "valid_spec.md", specs / "messages-table.md")
    shutil.copy(FIXTURES / "missing_spec_section.md", specs / "partial.md")
    return str(tmp_path / "vault")


def _load_graph(name: str) -> TaskGraph:
    path = FIXTURES / name
    if path.suffix == ".md":
        return extract_graph_from_spec(path.read_text(encoding="utf-8"), str(path))
    return parse_graph(path.read_text(encoding="utf-8"))


def _findings_signature(findings) -> list[dict]:
    return sorted(
        ({"rule": f.rule, "node": f.node, "severity": f.severity} for f in findings),
        key=lambda f: (f["rule"], f["node"] or "", f["severity"]),
    )


def _golden(name: str) -> list[dict]:
    return sorted(
        json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8")),
        key=lambda f: (f["rule"], f["node"] or "", f["severity"]),
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseGraph:
    def test_json_document(self):
        graph = _load_graph("valid.json")
        assert graph.version == 1
        assert graph.node_keys() == ["schema", "queries", "engine"]
        assert graph.parent.title == "Messages table + delivery engine"
        assert graph.vars == {"base": "main"}

    def test_defaults_fill_unset_node_fields(self):
        graph = _load_graph("valid.json")
        schema = graph.nodes[0]
        assert schema.profile == "coding"  # from defaults
        assert graph.nodes[1].labels == ["overhaul-b"]  # from defaults
        assert schema.labels == ["db"]  # node wins over defaults

    def test_needs_shorthand_defaults_to_blocks(self):
        graph = _load_graph("valid.json")
        engine = graph.nodes[2]
        assert engine.needs[0].on == "queries"
        assert engine.needs[0].dep_type == "blocks"
        assert engine.needs[0].cross_project is False

    def test_dict_source_accepted(self):
        graph = parse_graph({"version": 1, "nodes": [{"key": "a", "title": "A"}]})
        assert graph.node_keys() == ["a"]

    def test_yaml_source_accepted(self):
        graph = parse_graph("version: 1\nnodes:\n  - key: a\n    title: A\n")
        assert graph.node_keys() == ["a"]

    def test_empty_document_raises(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph("   ")
        assert exc.value.errors[0].rule == "empty_document"

    def test_no_nodes_raises(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph({"version": 1, "nodes": []})
        assert exc.value.errors[0].rule == "no_nodes"

    def test_missing_key_raises(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph({"version": 1, "nodes": [{"title": "no key"}]})
        assert exc.value.errors[0].rule == "missing_key"

    def test_unsupported_version_raises(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph({"version": 7, "nodes": [{"key": "a", "title": "A"}]})
        assert exc.value.errors[0].rule == "bad_version"

    def test_deep_nesting_is_a_finding_not_a_recursion_error(self):
        """``parser.py`` caught only JSONDecodeError/YAMLError, so a deeply
        nested document raised an uncaught RecursionError out of parse_graph."""
        bomb = "[" * 4000 + "]" * 4000
        with pytest.raises(GraphParseError) as exc:
            parse_graph(bomb)
        assert exc.value.errors  # structured, not a bare stack overflow

    def test_document_over_the_size_cap_is_rejected(self):
        from src.task_graph.parser import MAX_GRAPH_DOCUMENT_CHARS

        with pytest.raises(GraphParseError) as exc:
            parse_graph("x" * (MAX_GRAPH_DOCUMENT_CHARS + 1))
        assert [e.rule for e in exc.value.errors] == ["document_too_large"]

    def test_all_structural_errors_reported_at_once(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(
                {
                    "version": 1,
                    "nodes": [
                        {"key": "a", "title": "A", "priority": "high"},
                        {"key": "b", "title": "B", "needs": [{"dep_type": "blocks"}]},
                    ],
                }
            )
        rules = {e.rule for e in exc.value.errors}
        assert rules == {"bad_field_type", "bad_need"}


class TestSubtaskBoundsLiveWithTheTable:
    """The three per-row/per-call bounds are leaf constants, not command ones.

    ``src/task_graph/parser.py`` must be able to state the same numbers
    ``task_subtask_add`` enforces without importing ``src.commands`` — that
    package builds the whole ``CommandHandler`` at import, and the handler
    imports ``src.task_graph`` right back.
    """

    def test_the_bounds_are_reachable_without_importing_the_commands_package(self):
        """Module import plus the three constants, with ``src.commands`` unloaded.

        Scoped deliberately to the *bounds*: ``_parse_node`` still lazily
        imports ``normalize_deliverables`` from ``src.commands.task_commands``
        for the ``deliverables`` field, which is pre-existing and out of scope
        here — so this probe reads the constants rather than parsing.
        """
        import subprocess
        import sys

        probe = (
            "import sys;"
            "import src.task_graph.parser as p;"
            "assert 'src.commands' not in sys.modules, sorted("
            "m for m in sys.modules if m.startswith('src.commands'));"
            "assert not hasattr(p, '_subtask_bounds');"
            "print(p.MAX_SUBTASKS_PER_CALL, p.MAX_SUBTASK_TITLE, p.MAX_SUBTASK_CONTEXT)"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.split() == [
            str(MAX_SUBTASKS_PER_CALL),
            str(MAX_SUBTASK_TITLE),
            str(MAX_SUBTASK_CONTEXT),
        ]

    def test_the_commands_module_still_re_exports_them(self):
        """Their original home stays importable — callers and tests use it."""
        from src.commands import task_subtask_commands as cmds

        assert (
            cmds.MAX_SUBTASKS_PER_CALL,
            cmds.MAX_SUBTASK_TITLE,
            cmds.MAX_SUBTASK_CONTEXT,
        ) == (MAX_SUBTASKS_PER_CALL, MAX_SUBTASK_TITLE, MAX_SUBTASK_CONTEXT)

    def test_importing_the_commands_package_first_still_works(self):
        """The reverse import order must not deadlock on a partial module."""
        import subprocess
        import sys

        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import src.commands.task_subtask_commands;"
                "import src.task_graph.parser as p;"
                "print(p.MAX_SUBTASK_TITLE)",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == str(MAX_SUBTASK_TITLE)


class TestParseSubtasks:
    """``subtasks:`` on a node — the planning-emits-subtasks grammar (§7.2)."""

    @staticmethod
    def _doc(subtasks) -> dict:
        return {
            "version": 1,
            "nodes": [{"key": "a", "title": "A", "subtasks": subtasks}],
        }

    def test_three_string_entries_become_three_subtasks(self):
        graph = parse_graph(self._doc(["one", "two", "three"]))
        node = graph.nodes[0]
        assert [s.title for s in node.subtasks] == ["one", "two", "three"]
        assert [s.context for s in node.subtasks] == ["", "", ""]

    def test_mixed_string_and_object_entries_parse(self):
        graph = parse_graph(
            self._doc(["bare", {"title": "rich", "context": "why it matters"}])
        )
        node = graph.nodes[0]
        assert [(s.title, s.context) for s in node.subtasks] == [
            ("bare", ""),
            ("rich", "why it matters"),
        ]

    def test_subtasks_round_trip_through_to_dict(self):
        graph = parse_graph(self._doc([{"title": "t", "context": "c"}]))
        assert graph.to_dict()["nodes"][0]["subtasks"] == [{"title": "t", "context": "c"}]

    def test_a_bare_string_is_coerced_to_one_entry(self):
        graph = parse_graph(self._doc("just one"))
        assert [s.title for s in graph.nodes[0].subtasks] == ["just one"]

    def test_a_bare_object_is_coerced_to_one_entry(self):
        graph = parse_graph(self._doc({"title": "just one"}))
        assert [s.title for s in graph.nodes[0].subtasks] == ["just one"]

    def test_an_over_long_title_is_one_bad_subtask(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc(["x" * (MAX_SUBTASK_TITLE + 1)]))
        assert [e.rule for e in exc.value.errors] == ["bad_subtask"]
        assert exc.value.errors[0].node == "a"

    def test_an_empty_title_is_one_bad_subtask(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc(["   "]))
        assert [e.rule for e in exc.value.errors] == ["bad_subtask"]

    def test_an_over_long_context_is_one_bad_subtask(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc([{"title": "t", "context": "x" * (MAX_SUBTASK_CONTEXT + 1)}]))
        assert [e.rule for e in exc.value.errors] == ["bad_subtask"]

    @pytest.mark.parametrize("context", [0, False, [], {}, 5, ["a"], {"k": "v"}])
    def test_a_non_string_context_is_one_bad_subtask(self, context):
        """A *falsy* non-string is as wrong as a truthy one.

        ``raw.get("context", "") or ""`` accepted ``0``/``False``/``[]`` as an
        empty context while rejecting ``5``, which is the author's mistake
        being silently swallowed for half the type errors.
        """
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc([{"title": "t", "context": context}]))
        assert [e.rule for e in exc.value.errors] == ["bad_subtask"]
        assert "context" in exc.value.errors[0].detail

    @pytest.mark.parametrize("doc_context", [None, ...])
    def test_absent_or_null_context_is_the_empty_string(self, doc_context):
        """Only ``None`` and an omitted key mean "no context"."""
        entry = {"title": "t"} if doc_context is ... else {"title": "t", "context": None}
        graph = parse_graph(self._doc([entry]))
        assert graph.nodes[0].subtasks[0].context == ""

    def test_a_non_string_entry_is_one_bad_subtask(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc([17]))
        assert [e.rule for e in exc.value.errors] == ["bad_subtask"]

    def test_a_non_list_value_is_one_bad_subtask(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc(17))
        assert [e.rule for e in exc.value.errors] == ["bad_subtask"]

    def test_over_the_per_node_cap_is_one_bad_subtask(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc([f"item {i}" for i in range(MAX_SUBTASKS_PER_CALL + 1)]))
        assert [e.rule for e in exc.value.errors] == ["bad_subtask"]
        assert str(MAX_SUBTASKS_PER_CALL) in exc.value.errors[0].detail

    def test_exactly_the_per_node_cap_parses(self):
        graph = parse_graph(self._doc([f"item {i}" for i in range(MAX_SUBTASKS_PER_CALL)]))
        assert len(graph.nodes[0].subtasks) == MAX_SUBTASKS_PER_CALL

    def test_defaults_supply_subtasks_when_a_node_omits_them(self):
        graph = parse_graph(
            {
                "version": 1,
                "defaults": {"subtasks": ["shared"]},
                "nodes": [{"key": "a", "title": "A"}, {"key": "b", "title": "B"}],
            }
        )
        assert [s.title for s in graph.nodes[0].subtasks] == ["shared"]
        assert [s.title for s in graph.nodes[1].subtasks] == ["shared"]

    def test_a_document_without_subtasks_still_reports_an_empty_list(self):
        """The key is additive: today's documents keep parsing unchanged."""
        graph = _load_graph("valid.json")
        assert all(node.subtasks == [] for node in graph.nodes)
        assert all(n["subtasks"] == [] for n in graph.to_dict()["nodes"])


class TestParentSubtasks:
    """Container parents are not scheduled work and cannot own checklists."""

    @staticmethod
    def _doc(subtasks) -> dict:
        return {
            "version": 1,
            "parent": {"title": "Epic", "subtasks": subtasks},
            "nodes": [{"key": "a", "title": "A"}],
        }

    def test_nonempty_parent_subtasks_are_a_parse_finding(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc(["not scheduled work"]))
        assert [error.rule for error in exc.value.errors] == ["parent_subtasks_unsupported"]

    def test_an_empty_parent_subtasks_list_is_allowed_for_templates(self):
        graph = parse_graph(self._doc([]))
        assert graph.parent is not None
        assert graph.parent.title == "Epic"


class TestParsePhases:
    """``phases:`` at the top level and ``phase:`` on a node (§7.3)."""

    @staticmethod
    def _doc(phases, node_phase=None) -> dict:
        node = {"key": "a", "title": "A"}
        if node_phase is not None:
            node["phase"] = node_phase
        return {"version": 1, "phases": phases, "nodes": [node]}

    def test_phases_parse_in_document_order(self):
        graph = parse_graph(
            self._doc(
                [
                    {"key": "schema", "title": "Phase 1", "label": "schema"},
                    {"key": "engine", "title": "Phase 2"},
                ]
            )
        )
        assert [(p.key, p.title, p.label) for p in graph.phases] == [
            ("schema", "Phase 1", "schema"),
            ("engine", "Phase 2", None),
        ]

    def test_a_node_names_its_phase(self):
        graph = parse_graph(
            self._doc([{"key": "schema", "title": "Phase 1"}], node_phase="schema")
        )
        assert graph.nodes[0].phase == "schema"

    def test_phases_round_trip_through_to_dict(self):
        graph = parse_graph(
            self._doc([{"key": "schema", "title": "Phase 1", "label": "s"}], node_phase="schema")
        )
        payload = graph.to_dict()
        assert payload["phases"] == [{"key": "schema", "title": "Phase 1", "label": "s"}]
        assert payload["nodes"][0]["phase"] == "schema"
        assert parse_graph(payload).phases[0].key == "schema"

    def test_a_non_list_phases_value_is_one_bad_phase(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc({"key": "schema"}))
        assert [e.rule for e in exc.value.errors] == ["bad_phase"]

    def test_a_non_object_entry_is_one_bad_phase(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc(["schema"]))
        assert [e.rule for e in exc.value.errors] == ["bad_phase"]

    def test_a_phase_without_a_key_is_one_missing_phase_key(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc([{"title": "Phase 1"}]))
        assert [e.rule for e in exc.value.errors] == ["missing_phase_key"]

    def test_a_non_string_node_phase_is_one_bad_field_type(self):
        with pytest.raises(GraphParseError) as exc:
            parse_graph(self._doc([{"key": "schema", "title": "P"}], node_phase=7))
        assert [e.rule for e in exc.value.errors] == ["bad_field_type"]

    def test_a_document_without_phases_still_reports_an_empty_list(self):
        """The key is additive: today's documents keep parsing unchanged."""
        graph = _load_graph("valid.json")
        assert graph.phases == []
        assert graph.to_dict()["phases"] == []
        assert all(n["phase"] is None for n in graph.to_dict()["nodes"])


class TestPhaseValidation:
    """The §7.3 finding table: two errors and two warnings."""

    @staticmethod
    async def _findings(doc, vault):
        graph = parse_graph(doc)
        return await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)

    async def test_a_duplicate_phase_key_is_an_error(self, vault):
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "one", "title": "Again"}],
                "nodes": [{"key": "a", "title": "A", "acceptance": ["x"], "phase": "one"}],
            },
            vault,
        )
        matched = [f for f in findings if f.rule == "duplicate_phase_key"]
        assert len(matched) == 1
        assert matched[0].is_error is True

    async def test_a_node_naming_an_undeclared_phase_is_an_error(self, vault):
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}],
                "nodes": [{"key": "a", "title": "A", "acceptance": ["x"], "phase": "two"}],
            },
            vault,
        )
        matched = [f for f in findings if f.rule == "unknown_phase"]
        assert len(matched) == 1
        assert (matched[0].is_error, matched[0].node) == (True, "a")

    async def test_a_phase_with_no_nodes_is_a_warning(self, vault):
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
                "nodes": [{"key": "a", "title": "A", "acceptance": ["x"], "phase": "one"}],
            },
            vault,
        )
        matched = [f for f in findings if f.rule == "phase_without_nodes"]
        assert len(matched) == 1
        assert matched[0].severity == "warning"
        assert "two" in matched[0].detail

    async def test_an_edge_onto_an_earlier_phase_is_a_redundant_warning(self, vault):
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
                "nodes": [
                    {"key": "a", "title": "A", "acceptance": ["x"], "phase": "one"},
                    {
                        "key": "b",
                        "title": "B",
                        "acceptance": ["x"],
                        "phase": "two",
                        "needs": [{"on": "a"}],
                    },
                ],
            },
            vault,
        )
        matched = [f for f in findings if f.rule == "redundant_phase_edge"]
        assert len(matched) == 1
        assert (matched[0].severity, matched[0].node) == ("warning", "b")

    async def test_an_edge_onto_a_later_phase_is_an_error(self, vault):
        """A phase-inverted need is a permanent deadlock no cycle check sees."""
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "phase": "one",
                        "needs": [{"on": "b"}],
                    },
                    {"key": "b", "title": "B", "acceptance": ["x"], "phase": "two"},
                ],
            },
            vault,
        )
        matched = [f for f in findings if f.rule == "inverted_phase_edge"]
        assert len(matched) == 1
        assert (matched[0].is_error, matched[0].node) == (True, "a")
        assert "'one'" in matched[0].detail and "'two'" in matched[0].detail
        # Nothing else fires: the cycle check cannot see this at all.
        assert [f.rule for f in findings if f.is_error] == ["inverted_phase_edge"]

    @pytest.mark.parametrize(
        "dep_type", ["blocks", "conditional-blocks", "parent-child", "waits-for"]
    )
    async def test_every_gating_dep_type_inverts(self, vault, dep_type):
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "phase": "one",
                        "needs": [{"on": "b", "dep_type": dep_type}],
                    },
                    {"key": "b", "title": "B", "acceptance": ["x"], "phase": "two"},
                ],
            },
            vault,
        )
        assert [f.rule for f in findings if f.is_error] == ["inverted_phase_edge"]

    async def test_a_non_blocking_edge_across_phases_is_neither(self, vault):
        """``related`` does not gate, so it can neither deadlock nor be redundant."""
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "phase": "one",
                        "needs": [{"on": "b", "dep_type": "related"}],
                    },
                    {
                        "key": "b",
                        "title": "B",
                        "acceptance": ["x"],
                        "phase": "two",
                        "needs": [{"on": "a", "dep_type": "related"}],
                    },
                ],
            },
            vault,
        )
        assert [f.rule for f in findings] == []

    async def test_a_farther_reach_never_masks_a_nearer_inversion(self, vault):
        """Regression (F9): ``a`` inverts onto both phase two and phase three.

        While ``waits-for`` was a *soft* class, the farther soft reach won the
        ranking and the whole node was reported as a warning — so the hard
        ``a -> b`` deadlock went unreported and the graph was created.  Every
        gating type is an error now, so the ranking can only pick between
        errors.
        """
        findings = await self._findings(
            {
                "version": 1,
                "phases": [
                    {"key": "one", "title": "One"},
                    {"key": "two", "title": "Two"},
                    {"key": "three", "title": "Three"},
                ],
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "phase": "one",
                        "needs": [
                            {"on": "b", "dep_type": "blocks"},
                            {"on": "c", "dep_type": "waits-for"},
                        ],
                    },
                    {"key": "b", "title": "B", "acceptance": ["x"], "phase": "two"},
                    {"key": "c", "title": "C", "acceptance": ["x"], "phase": "three"},
                ],
            },
            vault,
        )
        matched = [f for f in findings if f.rule == "inverted_phase_edge"]
        assert len(matched) == 1
        assert matched[0].is_error is True
        assert [f.rule for f in findings if f.is_error] == ["inverted_phase_edge"]

    async def test_a_waits_for_target_can_be_given_children_by_the_document(self, vault):
        """Regression (F10): the 'a document cannot give a node children'
        premise was false — a node may declare a ``parent-child`` need, the
        creator writes that row, and ``_waits_for_unsat`` counts it.  So this
        is a real deadlock and must be an error."""
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "phase": "one",
                        "needs": [{"on": "b", "dep_type": "waits-for"}],
                    },
                    {"key": "b", "title": "B", "acceptance": ["x"], "phase": "two"},
                    {
                        "key": "c",
                        "title": "C",
                        "acceptance": ["x"],
                        "phase": "two",
                        "needs": [{"on": "b", "dep_type": "parent-child"}],
                    },
                ],
            },
            vault,
        )
        matched = [f for f in findings if f.rule == "inverted_phase_edge"]
        assert [(f.node, f.is_error) for f in matched] == [("a", True)]

    async def test_a_very_long_gating_chain_does_not_blow_the_stack(self, vault):
        """Regression (F11): the traversal was recursive, and the 2,000,000
        character document cap leaves room for a chain far past Python's
        recursion limit."""
        size = 5000
        nodes = [
            {
                "key": f"n{i}",
                "title": f"N{i}",
                "acceptance": ["x"],
                "needs": [{"on": f"n{i + 1}"}],
            }
            for i in range(size - 1)
        ]
        nodes.append({"key": f"n{size - 1}", "title": "last", "acceptance": ["x"]})
        nodes[0]["phase"] = "one"
        nodes[-1]["phase"] = "two"
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
                "nodes": nodes,
            },
            vault,
        )
        matched = [f for f in findings if f.rule == "inverted_phase_edge"]
        assert len(matched) == 1
        assert matched[0].node == "n0"
        # The route is folded rather than printed in full: 5,000 keys is not a
        # message a human reads.
        assert len(matched[0].detail) < 500
        assert f"n{size - 1}" in matched[0].detail

    @staticmethod
    def _chain(*hops: tuple[str, str | None]) -> dict:
        """A document whose nodes form one ``needs`` chain, first needs second.

        Each hop is ``(key, phase or None)``.
        """
        nodes = []
        for index, (key, phase) in enumerate(hops):
            node: dict = {"key": key, "title": key.upper(), "acceptance": ["x"]}
            if phase is not None:
                node["phase"] = phase
            if index + 1 < len(hops):
                node["needs"] = [{"on": hops[index + 1][0]}]
            nodes.append(node)
        # Only the phases the chain actually uses, so an unrelated
        # ``phase_without_nodes`` warning never muddies an assertion.
        used = [p for p in ("one", "two") if any(h[1] == p for h in hops)]
        return {
            "version": 1,
            "phases": [{"key": key, "title": key.title()} for key in used],
            "nodes": nodes,
        }

    async def test_a_later_phase_reached_through_one_unphased_hop_is_an_error(self, vault):
        """The unsound case: an unphased node is not withheld by any phase, but
        it is still gated by its OWN edges, so it carries the deadlock."""
        findings = await self._findings(self._chain(("a", "one"), ("u", None), ("b", "two")), vault)
        matched = [f for f in findings if f.rule == "inverted_phase_edge"]
        assert len(matched) == 1
        assert (matched[0].is_error, matched[0].node) == (True, "a")
        assert "a -> u -> b" in matched[0].detail
        assert "'one'" in matched[0].detail and "'two'" in matched[0].detail

    async def test_a_later_phase_reached_through_two_unphased_hops_is_an_error(self, vault):
        findings = await self._findings(
            self._chain(("a", "one"), ("u", None), ("v", None), ("b", "two")), vault
        )
        matched = [f for f in findings if f.rule == "inverted_phase_edge"]
        assert len(matched) == 1
        assert "a -> u -> v -> b" in matched[0].detail

    async def test_a_chain_back_into_the_same_phase_is_not_an_error(self, vault):
        findings = await self._findings(self._chain(("a", "one"), ("u", None), ("b", "one")), vault)
        assert [f.rule for f in findings] == []

    async def test_a_chain_from_a_later_phase_to_an_earlier_one_is_not_an_error(self, vault):
        """Phase 2 waiting on phase-1 work through an unphased hop is the
        ordinary direction: phase 1 finishes first anyway."""
        findings = await self._findings(self._chain(("a", "two"), ("u", None), ("b", "one")), vault)
        assert [f.rule for f in findings] == []

    async def test_an_unphased_node_needing_a_later_phase_is_not_itself_reported(self, vault):
        """Nothing gates an unphased node's *start*, so on its own it is fine;
        only a phased node that reaches through it deadlocks."""
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
                "nodes": [
                    {"key": "a", "title": "A", "acceptance": ["x"], "phase": "one"},
                    {
                        "key": "u",
                        "title": "U",
                        "acceptance": ["x"],
                        "needs": [{"on": "b"}],
                    },
                    {"key": "b", "title": "B", "acceptance": ["x"], "phase": "two"},
                ],
            },
            vault,
        )
        assert [f.rule for f in findings] == []

    async def test_a_phased_intermediate_is_reported_by_its_own_edge_only_once(self, vault):
        """``a`` (phase one) -> ``m`` (phase one) -> ``b`` (phase two): the
        violation belongs to ``m``, and ``a`` is not reported for reaching
        through it — propagation stops at a phased node."""
        findings = await self._findings(
            self._chain(("a", "one"), ("m", "one"), ("b", "two")), vault
        )
        matched = [f for f in findings if f.rule == "inverted_phase_edge"]
        assert [f.node for f in matched] == ["m"]

    async def test_an_edge_within_one_phase_is_not_redundant(self, vault):
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}],
                "nodes": [
                    {"key": "a", "title": "A", "acceptance": ["x"], "phase": "one"},
                    {
                        "key": "b",
                        "title": "B",
                        "acceptance": ["x"],
                        "phase": "one",
                        "needs": [{"on": "a"}],
                    },
                ],
            },
            vault,
        )
        assert [f for f in findings if f.rule == "redundant_phase_edge"] == []

    async def test_a_phased_graph_with_no_findings_validates_clean(self, vault):
        findings = await self._findings(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "One"}],
                "nodes": [{"key": "a", "title": "A", "acceptance": ["x"], "phase": "one"}],
            },
            vault,
        )
        assert findings == []


class TestExtractFromSpec:
    def test_extracts_the_fenced_block(self):
        markdown = (FIXTURES / "valid_spec.md").read_text(encoding="utf-8")
        body = extract_graph_block(markdown)
        assert body.startswith("version: 1")
        assert "```" not in body

    def test_spec_is_implied_from_the_path(self):
        graph = _load_graph("valid_spec.md")
        assert graph.from_spec is True
        assert graph.spec.endswith("valid_spec.md")
        assert graph.source_path.endswith("valid_spec.md")

    def test_no_block_raises(self):
        with pytest.raises(GraphParseError) as exc:
            extract_graph_from_spec("# Just prose\n", "spec.md")
        assert exc.value.errors[0].rule == "no_graph_block"

    def test_tilde_fences_supported(self):
        graph = extract_graph_from_spec(
            "~~~aq-graph\nversion: 1\nnodes:\n  - key: a\n    title: A\n~~~\n", "s.md"
        )
        assert graph.node_keys() == ["a"]

    def test_parent_subtasks_in_a_fenced_graph_are_rejected(self):
        with pytest.raises(GraphParseError) as exc:
            extract_graph_from_spec(
                "```aq-graph\n"
                "version: 1\n"
                "parent:\n"
                "  title: Epic\n"
                "  subtasks: [not-scheduled]\n"
                "nodes:\n"
                "  - key: a\n"
                "    title: A\n"
                "```\n",
                "parent-subtasks.md",
            )
        assert [error.rule for error in exc.value.errors] == ["parent_subtasks_unsupported"]


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------


class TestSubstituteVars:
    def test_expands_declared_vars_and_implicit_spec(self):
        graph = _load_graph("valid.json")
        used, unknown = substitute_vars(graph)
        assert unknown == set()
        assert used == {"base", "spec"}
        assert "{base}" not in graph.nodes[0].description
        assert graph.nodes[0].context[0].path == "projects/p1/specs/messages-table.md"

    def test_unknown_references_are_reported_and_left_alone(self):
        graph = parse_graph({"version": 1, "nodes": [{"key": "a", "title": "{nope}"}]})
        _used, unknown = substitute_vars(graph)
        assert unknown == {"nope"}
        assert graph.nodes[0].title == "{nope}"

    def test_json_braces_are_not_var_references(self):
        graph = parse_graph(
            {"version": 1, "nodes": [{"key": "a", "title": "A", "description": "use {} or {1}"}]}
        )
        _used, unknown = substitute_vars(graph)
        assert unknown == set()

    def test_running_twice_is_harmless(self):
        graph = _load_graph("valid.json")
        substitute_vars(graph)
        before = graph.nodes[0].description
        substitute_vars(graph)
        assert graph.nodes[0].description == before

    def test_a_var_whose_value_is_a_var_resolves_fully(self):
        """Single-pass expansion left a literal ``{b}`` in the created task."""
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"a": "{b}", "b": "boom"},
                "nodes": [{"key": "n", "title": "{a}"}],
            }
        )
        used, unknown = substitute_vars(graph)
        assert graph.nodes[0].title == "boom"
        assert unknown == set()
        # Both names count as used — `b` is referenced transitively, so
        # reporting it as unused_var was a false positive.
        assert {"a", "b"} <= used

    def test_circular_var_is_reported_not_silently_left_literal(self):
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"a": "{b}", "b": "{a}"},
                "nodes": [{"key": "n", "title": "{a}"}],
            }
        )
        _used, unknown = substitute_vars(graph)
        assert unknown & {"a", "b"}

    def test_parent_profile_is_expanded(self):
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"p": "coding"},
                "parent": {"title": "P", "profile": "{p}"},
                "nodes": [{"key": "n", "title": "N"}],
            }
        )
        used, unknown = substitute_vars(graph)
        assert graph.parent.profile == "coding"
        assert unknown == set()
        assert "p" in used

    def test_subtask_titles_and_contexts_are_expanded(self):
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"branch": "feat/x"},
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "subtasks": [{"title": "Rebase {branch}", "context": "onto {branch}"}],
                    }
                ],
            }
        )
        used, unknown = substitute_vars(graph)
        assert (used, unknown) == ({"branch"}, set())
        subtask = graph.nodes[0].subtasks[0]
        assert (subtask.title, subtask.context) == ("Rebase feat/x", "onto feat/x")

    def test_an_undeclared_var_in_a_subtask_is_reported(self):
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [{"key": "a", "title": "A", "subtasks": ["Rebase {nope}"]}],
            }
        )
        _used, unknown = substitute_vars(graph)
        assert unknown == {"nope"}

    def test_phase_titles_and_labels_are_expanded(self):
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"release": "24.1"},
                "phases": [{"key": "one", "title": "Ship {release}", "label": "{release}"}],
                "nodes": [{"key": "a", "title": "A", "phase": "one"}],
            }
        )
        used, unknown = substitute_vars(graph)
        assert (used, unknown) == ({"release"}, set())
        assert (graph.phases[0].title, graph.phases[0].label) == ("Ship 24.1", "24.1")

    def test_an_undeclared_var_in_a_phase_title_is_reported(self):
        graph = parse_graph(
            {
                "version": 1,
                "phases": [{"key": "one", "title": "Ship {nope}"}],
                "nodes": [{"key": "a", "title": "A", "phase": "one"}],
            }
        )
        _used, unknown = substitute_vars(graph)
        assert unknown == {"nope"}

    def test_context_type_is_expanded(self):
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"kind": "spec_ref"},
                "nodes": [
                    {"key": "n", "title": "N", "context": [{"type": "{kind}", "path": "x.md"}]}
                ],
            }
        )
        used, unknown = substitute_vars(graph)
        assert graph.nodes[0].context[0].type == "spec_ref"
        assert unknown == set()
        assert "kind" in used


class TestVarSubstitutionThroughValidation:
    async def test_chained_var_produces_no_spurious_findings(self, vault):
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"a": "{b}", "b": "boom"},
                "nodes": [{"key": "n", "title": "{a}", "acceptance": ["x"]}],
            }
        )
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        assert [f.rule for f in findings] == []
        assert graph.nodes[0].title == "boom"

    async def test_var_in_parent_profile_is_accepted(self, vault):
        """`unused_var 'p'` + `unknown_profile '{p}'` on a correct document."""
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"p": "coding"},
                "parent": {"title": "P", "profile": "{p}"},
                "nodes": [{"key": "n", "title": "N", "acceptance": ["x"]}],
            }
        )
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        assert [f.rule for f in findings] == []
        assert graph.parent.profile == "coding"


# ---------------------------------------------------------------------------
# Self-edges (all dep types)
# ---------------------------------------------------------------------------


class TestSelfEdges:
    @pytest.mark.parametrize("dep_type", ["blocks", "related", "parent-child", "waits-for"])
    async def test_self_edge_is_rejected_for_every_dep_type(self, dep_type, vault):
        """A non-blocking self-edge used to validate clean, then die at insert
        against ``CheckConstraint("task_id != depends_on_task_id")``."""
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "needs": [{"on": "a", "dep_type": dep_type}],
                    }
                ],
            }
        )
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        errors, _warnings = split_findings(findings)
        assert "self_edge" in {f.rule for f in errors}
        assert [f.node for f in errors if f.rule == "self_edge"] == ["a"]

    async def test_a_genuine_two_node_edge_is_untouched(self, vault):
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [
                    {"key": "a", "title": "A", "acceptance": ["x"]},
                    {
                        "key": "b",
                        "title": "B",
                        "acceptance": ["x"],
                        "needs": [{"on": "a", "dep_type": "related"}],
                    },
                ],
            }
        )
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        assert [f.rule for f in findings] == []


# ---------------------------------------------------------------------------
# Profile scoping
# ---------------------------------------------------------------------------


class TestProfileScoping:
    """Project-scoped profiles were retired — the form itself is now an error."""

    async def test_scoped_profile_reference_is_rejected(self, vault):
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [
                    {"key": "a", "title": "A", "acceptance": ["x"], "profile": "project:p2:coding"}
                ],
            }
        )
        db = _FakeDB(profiles={"coding", "project:p2:coding"})
        findings = await validate_graph(graph, project_id="p1", db=db, vault_root=vault)
        errors, _ = split_findings(findings)
        assert {f.rule for f in errors} == {"retired_project_profile"}
        assert graph.nodes[0].profile == "project:p2:coding"  # never rewritten

    async def test_scoped_parent_profile_is_rejected(self, vault):
        graph = parse_graph(
            {
                "version": 1,
                "parent": {"title": "P", "profile": "project:p2:coding"},
                "nodes": [{"key": "a", "title": "A", "acceptance": ["x"]}],
            }
        )
        db = _FakeDB(profiles={"project:p2:coding"})
        findings = await validate_graph(graph, project_id="p1", db=db, vault_root=vault)
        assert {f.rule for f in split_findings(findings)[0]} == {"retired_project_profile"}

    async def test_a_scoped_reference_to_this_project_is_rejected_too(self, vault):
        """Even the graph's own project: the override it names no longer exists."""
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [
                    {"key": "a", "title": "A", "acceptance": ["x"], "profile": "project:p1:special"}
                ],
            }
        )
        db = _FakeDB(profiles={"project:p1:special"})
        findings = await validate_graph(graph, project_id="p1", db=db, vault_root=vault)
        assert {f.rule for f in split_findings(findings)[0]} == {"retired_project_profile"}


# ---------------------------------------------------------------------------
# spec_ref containment (arbitrary-file-read into another agent's prompt)
# ---------------------------------------------------------------------------


class TestSpecRefContainment:
    """``spec_ref`` paths must stay inside the vault.

    The graph is authored by an LLM from vault specs whose text may be
    attacker-influenced, and the resolved file is inlined verbatim into
    another agent's prime document by ``src/prime/sections._render_spec_ref``.
    """

    @staticmethod
    def _graph_with_ref(path: str):
        return parse_graph(
            {
                "version": 1,
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "context": [{"type": "spec_ref", "path": path}],
                    }
                ],
            }
        )

    @pytest.fixture
    def secret(self, vault, tmp_path):
        """A real file next to the vault, i.e. outside it."""
        target = tmp_path / "secret.md"
        target.write_text("## Secret\nsk-do-not-leak\n", encoding="utf-8")
        return target

    async def test_dotdot_traversal_is_an_error(self, vault, secret):
        graph = self._graph_with_ref("../secret.md")
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        errors, _ = split_findings(findings)
        assert {f.rule for f in errors} == {"spec_ref_outside_vault"}

    async def test_absolute_path_outside_the_vault_is_an_error(self, vault, secret):
        graph = self._graph_with_ref(str(secret))
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        errors, _ = split_findings(findings)
        assert {f.rule for f in errors} == {"spec_ref_outside_vault"}

    async def test_symlink_pointing_outside_the_vault_is_an_error(self, vault, secret):
        link = Path(vault) / "projects" / "p1" / "specs" / "escape.md"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this host")
        graph = self._graph_with_ref("projects/p1/specs/escape.md")
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        errors, _ = split_findings(findings)
        assert {f.rule for f in errors} == {"spec_ref_outside_vault"}

    async def test_an_in_vault_reference_still_resolves(self, vault):
        graph = self._graph_with_ref("projects/p1/specs/messages-table.md")
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        assert [f.rule for f in findings] == []

    async def test_an_in_vault_absolute_reference_still_resolves(self, vault):
        spec = Path(vault) / "projects" / "p1" / "specs" / "messages-table.md"
        graph = self._graph_with_ref(str(spec))
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        assert [f.rule for f in findings] == []

    def test_resolve_spec_path_refuses_escapes(self, vault, secret):
        from src.task_graph.validator import resolve_spec_path, resolve_spec_path_checked

        for path in ("../secret.md", str(secret)):
            assert resolve_spec_path(path, vault_root=vault, source_path=None) is None
            assert resolve_spec_path_checked(path, vault_root=vault, source_path=None) == (
                None,
                "outside_vault",
            )

    async def test_traversal_is_refused_even_when_the_target_does_not_exist(self, vault):
        """Containment is decided before existence, so a traversal attempt is
        reported as one rather than as a benign 'file not found'."""
        graph = self._graph_with_ref("../../not/here/at/all.md")
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        errors, _ = split_findings(findings)
        assert {f.rule for f in errors} == {"spec_ref_outside_vault"}

    def test_resolve_spec_path_distinguishes_missing_from_escaping(self, vault):
        from src.task_graph.validator import resolve_spec_path_checked

        assert resolve_spec_path_checked("nope.md", vault_root=vault, source_path=None) == (
            None,
            "not_found",
        )


# ---------------------------------------------------------------------------
# Golden validation cases (§8.3 rule table)
# ---------------------------------------------------------------------------


GOLDEN_CASES = [
    "valid.json",
    "valid_spec.md",
    "unknown_var.json",
    "unused_var.json",
    "duplicate_key.json",
    "cycle.json",
    "non_blocking_loop.json",
    "unknown_profile.json",
    "bad_dep_type.json",
    "unresolved_need.json",
    "cross_project.json",
    "cross_project_allowed.json",
    "foreign_project_node.json",
    "missing_title.json",
    "no_acceptance.json",
    "missing_spec_section.md",
    "missing_spec_path_graph.json",
]


@pytest.mark.parametrize("case", GOLDEN_CASES)
async def test_validation_matches_golden(case, vault):
    """Every §8.3 rule has a fixture and a frozen expected finding list."""
    path = FIXTURES / case
    if path.suffix == ".md":
        # Validate the copy that lives inside the vault so spec_ref paths
        # resolve the way they do in production.
        installed = {
            "valid_spec.md": "messages-table.md",
            "missing_spec_section.md": "partial.md",
        }[case]
        spec_file = Path(vault) / "projects" / "p1" / "specs" / installed
        graph = extract_graph_from_spec(spec_file.read_text(encoding="utf-8"), str(spec_file))
    else:
        graph = parse_graph(path.read_text(encoding="utf-8"))

    findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
    assert _findings_signature(findings) == _golden(path.stem)


class TestValidatorDetails:
    async def test_a_bare_reference_no_longer_falls_back_to_an_override(self, vault):
        """Only the global profile counts; a leftover override row is not one."""
        graph = parse_graph(
            {"version": 1, "nodes": [{"key": "a", "title": "A", "profile": "special"}]}
        )
        db = _FakeDB(profiles={"project:p1:special"})
        findings = await validate_graph(graph, project_id="p1", db=db, vault_root=vault)
        assert {f.rule for f in findings if f.is_error} == {"unknown_profile"}

    async def test_system_profile_reference_is_left_alone(self, vault):
        graph = parse_graph(
            {"version": 1, "nodes": [{"key": "a", "title": "A", "profile": "coding"}]}
        )
        await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        assert graph.nodes[0].profile == "coding"

    async def test_existing_task_in_same_project_resolves(self, vault):
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [{"key": "a", "title": "A", "acceptance": ["x"], "needs": ["local-task"]}],
            }
        )
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        assert [f.rule for f in findings if f.is_error] == []

    async def test_spec_section_match_is_whitespace_and_case_insensitive(self, vault):
        spec = Path(vault) / "projects" / "p1" / "specs" / "messages-table.md"
        graph = parse_graph(
            {
                "version": 1,
                "spec": str(spec),
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "context": [{"type": "spec_ref", "section": "3.   schema"}],
                    }
                ],
            }
        )
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        assert [f.rule for f in findings if f.is_error] == []

    async def test_split_findings(self, vault):
        graph = parse_graph(
            {
                "version": 1,
                "vars": {"unused": "x"},
                "nodes": [{"key": "a", "title": "{ghost}"}],
            }
        )
        findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
        errors, warnings = split_findings(findings)
        assert {f.rule for f in errors} == {"unknown_var"}
        assert {f.rule for f in warnings} == {"unused_var", "no_acceptance"}


async def test_validator_reports_blocking_cycle_but_allows_related_cycle(vault):
    """§8.3: only blocking dep types (BLOCKING_DEP_TYPES) can form a forbidden
    cycle — an informational `related` cycle must not raise the `cycle` rule."""

    def two_node_cycle(dep_type: str) -> TaskGraph:
        return parse_graph(
            {
                "version": 1,
                "nodes": [
                    {
                        "key": "a",
                        "title": "A",
                        "acceptance": ["x"],
                        "needs": [{"on": "b", "dep_type": dep_type}],
                    },
                    {
                        "key": "b",
                        "title": "B",
                        "acceptance": ["x"],
                        "needs": [{"on": "a", "dep_type": dep_type}],
                    },
                ],
            }
        )

    blocking = await validate_graph(
        two_node_cycle("conditional-blocks"), project_id="p1", db=_FakeDB(), vault_root=vault
    )
    cycle_errors = [f for f in blocking if f.rule == "cycle"]
    assert len(cycle_errors) == 1
    assert cycle_errors[0].is_error
    assert "a" in cycle_errors[0].detail and "b" in cycle_errors[0].detail

    related = await validate_graph(
        two_node_cycle("related"), project_id="p1", db=_FakeDB(), vault_root=vault
    )
    assert [f.rule for f in related if f.rule == "cycle"] == []


# ---------------------------------------------------------------------------
# Creator
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    from src.database import Database
    from src.models import AgentProfile, Project

    database = Database(lease_dsn("graph.db"))
    await database.initialize()
    await database.create_project(Project(id="p1", name="p1"))
    # tasks.profile_id is a real FK — a graph referencing a profile that
    # isn't in agent_profiles would fail at insert, which is exactly what
    # the validator's unknown_profile rule exists to catch first.
    for profile_id in ("coding", "planner", "reviewer"):
        await database.create_profile(AgentProfile(id=profile_id, name=profile_id))
    yield database
    await database.close()


class _Handler:
    def __init__(self, db):
        self.db = db


async def _valid_graph(vault) -> TaskGraph:
    spec = Path(vault) / "projects" / "p1" / "specs" / "messages-table.md"
    graph = parse_graph((FIXTURES / "valid.json").read_text(encoding="utf-8"))
    graph.spec = str(spec)
    findings = await validate_graph(graph, project_id="p1", db=_FakeDB(), vault_root=vault)
    assert not [f for f in findings if f.is_error], findings
    return graph


class TestCreateGraph:
    async def test_creates_parent_nodes_deps_and_context(self, db, vault):
        graph = await _valid_graph(vault)
        report = await create_graph(_Handler(db), graph, project_id="p1")

        parent = await db.get_task(report["parent_id"])
        assert parent is not None
        assert parent.title == "Messages table + delivery engine"

        assert len(report["task_ids"]) == 3
        for task_id in report["task_ids"]:
            task = await db.get_task(task_id)
            assert task is not None
            assert task.parent_task_id == report["parent_id"]
            assert task.project_id == "p1"

        ids = {node["key"]: node["task_id"] for node in report["nodes"]}
        deps = await db.get_dependencies(ids["queries"])
        # ``set_parent`` (write_plan) writes a ``parent-child`` edge for
        # every node alongside the ``blocks`` edge the graph declared.
        assert deps == {ids["schema"], report["parent_id"]}

    @pytest.mark.parametrize(
        "mode, expected_repo_id",
        [("development", "graph-repo"), ("observe", "graph-repo"), ("disabled", None)],
    )
    @pytest.mark.parametrize("phased", [False, True])
    async def test_graph_rows_use_designated_repository(
        self, db, tmp_path, mode, expected_repo_id, phased
    ):
        from src.models import RepoConfig, RepoSourceType

        await db.create_repo(
            RepoConfig(
                id="graph-repo",
                project_id="p1",
                source_type=RepoSourceType.LINK,
                source_path=str(tmp_path / "repo"),
            )
        )
        await db.update_project(
            "p1",
            hierarchical_integration_mode=mode,
            integration_repository_id="graph-repo",
        )
        graph_doc = {
            "version": 1,
            "parent": {"title": "Epic"},
            "nodes": [
                {"key": "a", "title": "A", "phase": "one" if phased else None},
                {"key": "b", "title": "B"},
            ],
        }
        if phased:
            graph_doc["phases"] = [{"key": "one", "title": "Phase 1"}]

        report = await create_graph(_Handler(db), parse_graph(graph_doc), project_id="p1")
        task_ids = [report["parent_id"], *report["task_ids"]]
        task_ids.extend(phase["task_id"] for phase in report.get("phases", []))
        for task_id in task_ids:
            assert (await db.get_task(task_id)).repo_id == expected_repo_id

    async def test_dependency_rows_carry_dep_type(self, db, vault):
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [
                    {"key": "a", "title": "A", "acceptance": ["x"]},
                    {
                        "key": "b",
                        "title": "B",
                        "acceptance": ["x"],
                        "needs": [{"on": "a", "dep_type": "waits-for"}],
                    },
                ],
            }
        )
        report = await create_graph(_Handler(db), graph, project_id="p1")
        ids = {n["key"]: n["task_id"] for n in report["nodes"]}

        from sqlalchemy import select

        from src.database.tables import task_dependencies

        async with db._engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        select(task_dependencies).where(task_dependencies.c.task_id == ids["b"])
                    )
                )
                .mappings()
                .fetchall()
            )
        # Plus the ``parent-child`` edge ``set_parent`` writes for every node.
        assert sorted(r["dep_type"] for r in rows) == ["parent-child", "waits-for"]

    async def test_spec_ref_context_content_shape(self, db, vault):
        graph = await _valid_graph(vault)
        report = await create_graph(_Handler(db), graph, project_id="p1")
        schema_id = next(n["task_id"] for n in report["nodes"] if n["key"] == "schema")

        contexts = await db.get_task_contexts(schema_id)
        spec_refs = [c for c in contexts if c["type"] == "spec_ref"]
        assert len(spec_refs) == 1
        payload = json.loads(spec_refs[0]["content"])
        assert payload["section"] == "3. Schema"
        assert payload["path"].endswith("messages-table.md")

        files = [c for c in contexts if c["type"] == "file"]
        assert files[0]["content"] == "src/database/tables.py"

    async def test_acceptance_stored_as_criteria_and_in_description(self, db, vault):
        graph = await _valid_graph(vault)
        report = await create_graph(_Handler(db), graph, project_id="p1")
        schema_id = next(n["task_id"] for n in report["nodes"] if n["key"] == "schema")

        from sqlalchemy import select

        from src.database.tables import task_criteria

        async with db._engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        select(task_criteria)
                        .where(task_criteria.c.task_id == schema_id)
                        .order_by(task_criteria.c.sort_order)
                    )
                )
                .mappings()
                .fetchall()
            )
        assert [r["content"] for r in rows] == [
            "alembic upgrade head clean on both backends",
            "pytest tests/test_database.py green",
        ]
        task = await db.get_task(schema_id)
        assert "## Acceptance Criteria" in task.description

    async def test_labels_are_written(self, db, vault):
        graph = await _valid_graph(vault)
        report = await create_graph(_Handler(db), graph, project_id="p1")
        schema_id = next(n["task_id"] for n in report["nodes"] if n["key"] == "schema")
        assert await db.get_task_labels(schema_id) == ["db"]

    async def test_parent_and_node_statuses(self, db, vault):
        from src.models import TaskStatus

        graph = await _valid_graph(vault)
        report = await create_graph(_Handler(db), graph, project_id="p1")
        parent = await db.get_task(report["parent_id"])
        assert parent.status == TaskStatus.IN_PROGRESS
        for task_id in report["task_ids"]:
            assert (await db.get_task(task_id)).status == TaskStatus.DEFINED

    async def test_dry_run_writes_nothing_but_reports_ids(self, db, vault):
        graph = await _valid_graph(vault)
        report = await create_graph(_Handler(db), graph, project_id="p1", dry_run=True)
        assert report["dry_run"] is True
        assert report["created"] is False
        assert len(report["task_ids"]) == 3
        assert await db.get_task(report["parent_id"]) is None
        assert await db.list_tasks(project_id="p1") == []

    async def test_dry_run_resolves_needs_to_assigned_ids(self, db, vault):
        graph = await _valid_graph(vault)
        report = await create_graph(_Handler(db), graph, project_id="p1", dry_run=True)
        ids = {n["key"]: n["task_id"] for n in report["nodes"]}
        queries = next(n for n in report["nodes"] if n["key"] == "queries")
        assert queries["needs"][0]["task_id"] == ids["schema"]

    async def test_failure_on_node_three_leaves_zero_rows(self, db, vault, monkeypatch):
        """The single-transaction guarantee (§12): all or nothing."""
        import src.task_graph.creator as creator

        graph = await _valid_graph(vault)
        plan = await build_plan(db, graph, project_id="p1")

        calls = {"n": 0}
        real_insert = creator._insert_task

        async def flaky_insert(conn, row):
            calls["n"] += 1
            if calls["n"] == 3:  # parent, node 1, then blow up on node 2
                raise RuntimeError("injected failure")
            await real_insert(conn, row)

        monkeypatch.setattr(creator, "_insert_task", flaky_insert)

        with pytest.raises(RuntimeError, match="injected failure"):
            await write_plan(db, plan)

        assert await db.list_tasks(project_id="p1") == []
        assert await db.get_task(plan.parent_id) is None

    async def test_duplicate_labels_do_not_break_the_insert(self, db, vault):
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [
                    {"key": "a", "title": "A", "acceptance": ["x"], "labels": ["dup", "dup"]}
                ],
            }
        )
        report = await create_graph(_Handler(db), graph, project_id="p1")
        assert await db.get_task_labels(report["task_ids"][0]) == ["dup"]

    async def test_external_task_dependency_is_kept_verbatim(self, db, vault):
        from src.models import Task

        await db.create_task(Task(id="upstream", project_id="p1", title="U", description="U"))
        graph = parse_graph(
            {
                "version": 1,
                "nodes": [{"key": "a", "title": "A", "acceptance": ["x"], "needs": ["upstream"]}],
            }
        )
        report = await create_graph(_Handler(db), graph, project_id="p1")
        assert await db.get_dependencies(report["task_ids"][0]) == {"upstream", report["parent_id"]}
