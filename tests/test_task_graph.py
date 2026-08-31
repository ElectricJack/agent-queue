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
from src.task_graph.creator import build_plan, write_plan
from src.task_graph.models import TaskGraph

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
    async def test_another_projects_scoped_profile_is_rejected(self, vault):
        """`project:p2:coding` in a p1 graph validated clean and was written
        verbatim into ``tasks.profile_id``."""
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
        assert {f.rule for f in errors} == {"foreign_project_profile"}
        assert graph.nodes[0].profile == "project:p2:coding"  # never rewritten

    async def test_foreign_scoped_parent_profile_is_rejected(self, vault):
        graph = parse_graph(
            {
                "version": 1,
                "parent": {"title": "P", "profile": "project:p2:coding"},
                "nodes": [{"key": "a", "title": "A", "acceptance": ["x"]}],
            }
        )
        db = _FakeDB(profiles={"project:p2:coding"})
        findings = await validate_graph(graph, project_id="p1", db=db, vault_root=vault)
        assert {f.rule for f in split_findings(findings)[0]} == {"foreign_project_profile"}

    async def test_own_scoped_profile_resolves_idempotently(self, vault):
        """Writing the fully-scoped id yourself must not double-prefix."""
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
        assert [f.rule for f in findings] == []
        assert graph.nodes[0].profile == "project:p1:special"


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
    async def test_project_scoped_profile_override_resolves(self, vault):
        graph = parse_graph(
            {"version": 1, "nodes": [{"key": "a", "title": "A", "profile": "special"}]}
        )
        db = _FakeDB(profiles={"project:p1:special"})
        findings = await validate_graph(graph, project_id="p1", db=db, vault_root=vault)
        assert [f.rule for f in findings if f.is_error] == []
        # The reference is rewritten to the id that exists, so the created
        # task's profile_id FK names a real row.
        assert graph.nodes[0].profile == "project:p1:special"

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

    database = Database(str(tmp_path / "graph.db"))
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
