"""Batch automatic checks (held-batches spec L1.3, design §5.1).

Pure tests build a :class:`BatchGraph` from a payload (or stored member rows)
and a :class:`LiveGraph` by hand: one test per hard code, per retryable code
and per advisory finding.  The command tests run ``task_batch_validate``
against a disposable database.
"""

from __future__ import annotations

import time

import pytest

from src.database.tables import ingest_batches
from src.task_graph import batch_checks
from src.task_graph.batch_checks import (
    ADVISORY_CODES,
    HARD_CODES,
    RETRYABLE_CODES,
    Authority,
    BatchCheckFailed,
    BatchGraph,
    LiveGraph,
    check,
)

AUTH = Authority("p1", "spec:doc")


def _leaf(tid: str, **fields) -> dict:
    """A leaf carrying every field the checks read, so it raises nothing."""
    spec = {
        "tempId": tid,
        "title": f"Task {tid}",
        "section_anchor": f"§{tid}",
        "primary_outcome": f"{tid} lands",
        "acceptance": f"`aq test tests/test_{tid}.py` passes",
        "size": "s",
        "owned_modules": [f"src/{tid}.py"],
    }
    spec.update(fields)
    return spec


def _container(tid: str, **fields) -> dict:
    spec = {"tempId": tid, "title": f"Epic {tid}", "section_anchor": f"§{tid}"}
    spec.update(fields)
    return spec


def _edge(frm: str, to: str, kind: str = "blocks", reason: str | None = "needs its API") -> dict:
    edge = {"from": frm, "to": to, "dep_type": kind}
    if reason is not None:
        edge["reason"] = reason
    return edge


def _live(**fields) -> LiveGraph:
    return LiveGraph(project_id="p1", **fields)


def _check(payload: dict, live: LiveGraph | None = None, **kw) -> batch_checks.CheckReport:
    payload = {"project_id": "p1", "source": "spec:doc", **payload}
    return check(BatchGraph.from_payload(payload), live or _live(), authority=AUTH, **kw)


def _findings(report, code: str) -> list:
    return [f for f in report.findings if f.code == code]


def _one(report, code: str):
    found = _findings(report, code)
    assert len(found) == 1, report.to_dict()
    return found[0]


def test_code_lists_are_disjoint_and_complete():
    assert len(set(HARD_CODES)) == 15
    assert len(set(ADVISORY_CODES)) == 7
    assert not set(HARD_CODES) & set(RETRYABLE_CODES)
    assert not (set(HARD_CODES) | set(RETRYABLE_CODES)) & set(ADVISORY_CODES)


def test_clean_batch_passes_with_no_findings_or_gaps():
    report = _check({
        "tasks": [
            _container("E"),
            _leaf("a", parent_id="E"),
            _leaf("b", parent_id="E"),
        ],
        "edges": [_edge("b", "a")],
    })
    assert report.passed
    assert report.findings == ()
    assert report.gaps == ()
    assert report.to_dict() == {
        "passed": True, "retryable": False, "hard": [], "advisory": [], "gaps": [],
    }


# ---------------------------------------------------------------------------
# Hard codes
# ---------------------------------------------------------------------------


def test_authority_refuses_a_payload_claiming_another_project_or_source():
    report = check(
        BatchGraph.from_payload({"project_id": "p2", "source": "spec:other",
                                 "tasks": [_leaf("a")]}),
        _live(), authority=AUTH,
    )
    found = _findings(report, "authority")
    assert {f.data["declared"] for f in found} == {"p2", "spec:other"}
    assert not report.passed and not report.retryable


def test_authority_refuses_a_stored_batch_from_another_source():
    report = _check({"tasks": [_leaf("a")]}, _live(batch_project_id="p1",
                                                   batch_source="spec:elsewhere"))
    assert _one(report, "authority").data == {
        "declared": "spec:elsewhere", "authenticated": "spec:doc",
    }


def test_membership_refuses_a_parent_outside_the_batch():
    live = _live(tasks={"E1": {"status": "DEFINED", "title": "Existing"}})
    report = _check({"tasks": [_leaf("a", parent_id="E1")]}, live)
    assert _one(report, "membership").tasks == ("a", "E1")


def test_membership_refuses_edits_edges_comments_and_removals_on_non_members():
    live = _live(tasks={"X": {"status": "READY", "title": "Outside"},
                        "Y": {"status": "READY", "title": "Other"}})
    report = _check({
        "tasks": [_leaf("a")],
        "edges": [_edge("X", "a")],
        "edits": [{"task_id": "X", "title": "renamed"}],
        "comments": [{"task_id": "X", "body": "note"}],
        "remove_edges": [{"from": "X", "to": "Y", "dep_type": "blocks"}],
    }, live)
    details = [f.detail for f in _findings(report, "membership")]
    assert len(details) == 4
    assert any(d.startswith("edge from X") for d in details)
    assert any(d.startswith("edit targets X") for d in details)
    assert any(d.startswith("comment targets X") for d in details)
    assert any(d.startswith("edge removal from X") for d in details)


def test_membership_allows_stored_batch_members():
    live = _live(tasks={"M": {"status": "DEFINED", "title": "Member"}}, members={"M"})
    report = _check({"tasks": [_leaf("a")], "edits": [{"task_id": "M", "title": "x"}],
                     "comments": [{"task_id": "M", "body": "b"}]}, live)
    assert not _findings(report, "membership")


def test_parent_not_unique():
    report = _check({
        "tasks": [_container("C1"), _container("C2"), _leaf("a", parent_id="C1")],
        "edges": [_edge("a", "C2", "parent-child")],
    })
    assert _one(report, "parent_not_unique").tasks == ("a", "C1", "C2")


def test_cross_project_reference():
    live = _live(foreign={"X": "p2"})
    report = _check({"tasks": [_leaf("a")], "edges": [_edge("a", "X")]}, live)
    found = _one(report, "cross_project")
    assert found.tasks == ("X",) and "p2" in found.detail


def test_depth_exceeded():
    report = _check({"tasks": [
        _container("A"),
        _container("B", parent_id="A"),
        _container("C", parent_id="B"),
        _leaf("d", parent_id="C"),
    ]})
    found = _one(report, "depth_exceeded")
    assert found.tasks == ("d", "C", "B", "A")


def test_depth_counts_live_ancestors():
    live = _live(tasks={
        "R": {"status": "DEFINED", "parent_task_id": None, "title": "R"},
        "S": {"status": "DEFINED", "parent_task_id": "R", "title": "S"},
    }, members={"R", "S"})
    report = _check({"tasks": [_container("T", parent_id="S"), _leaf("u", parent_id="T")]},
                    live)
    assert _one(report, "depth_exceeded").tasks == ("u", "T", "S", "R")


def test_cycle_in_batch_edges():
    report = _check({"tasks": [_leaf("a"), _leaf("b")],
                     "edges": [_edge("a", "b"), _edge("b", "a")]})
    found = _one(report, "cycle")
    assert found.tasks == ("a", "b")
    assert set(found.edges) == {("a", "b", "blocks"), ("b", "a", "blocks")}


def test_cycle_through_live_edges():
    live = _live(tasks={"X": {"status": "READY", "title": "X"},
                        "Y": {"status": "READY", "title": "Y"}},
                 edges={("X", "Y", "blocks")})
    report = _check({"tasks": [_leaf("a")],
                     "edges": [_edge("Y", "a"), _edge("a", "X")]},
                    _live(tasks=live.tasks, edges=live.edges, members={"Y"}))
    assert set(_one(report, "cycle").tasks) == {"a", "X", "Y"}


def test_removed_live_edge_breaks_a_cycle():
    live = _live(tasks={"X": {"status": "READY", "title": "X"}},
                 edges={("X", "M", "blocks")}, members={"M", "X"})
    live.tasks["M"] = {"status": "DEFINED", "title": "M"}
    payload = {"tasks": [], "edges": [_edge("M", "X")]}
    assert _findings(_check(payload, live), "cycle")
    payload["remove_edges"] = [{"from": "X", "to": "M", "dep_type": "blocks"}]
    assert not _findings(_check(payload, live), "cycle")


def test_container_endpoint_on_blocks_edge():
    report = _check({
        "tasks": [_container("C"), _leaf("a", parent_id="C"), _leaf("b")],
        "edges": [_edge("b", "C")],
    })
    found = _one(report, "container_endpoint")
    assert found.tasks == ("C",)
    assert found.edges == (("b", "C", "blocks"),)


def test_container_endpoint_uses_live_container_metadata():
    live = _live(tasks={"K": {"status": "DEFINED", "title": "K"}}, containers={"K"})
    report = _check({"tasks": [_leaf("a")], "edges": [_edge("a", "K")]}, live)
    assert _one(report, "container_endpoint").tasks == ("K",)


def test_fan_in_kinds_may_target_a_container():
    report = _check({
        "tasks": [_container("C"), _leaf("a", parent_id="C"), _leaf("b"),
                  _leaf("r", placement="operational_root")],
        "edges": [_edge("b", "C", "waits-for"), _edge("r", "C", "delivery-of")],
    })
    assert not _findings(report, "container_endpoint")
    assert report.passed, report.to_dict()


def test_fan_in_kinds_still_need_a_leaf_dependent():
    report = _check({
        "tasks": [_container("C"), _leaf("a", parent_id="C"), _leaf("b")],
        "edges": [_edge("C", "b", "waits-for")],
    })
    assert _one(report, "container_endpoint").tasks == ("C",)


def test_external_target_missing():
    report = _check({"tasks": [_leaf("a")], "edges": [_edge("a", "ghost")]})
    assert _one(report, "external_target_invalid").detail == "ghost does not exist"


def test_external_target_archived():
    report = _check({"tasks": [_leaf("a")], "edges": [_edge("a", "old")]},
                    _live(archived={"old"}))
    assert _one(report, "external_target_invalid").detail == "old is archived"


def test_external_target_failed():
    live = _live(tasks={"F": {"status": "FAILED", "title": "F"}})
    report = _check({"tasks": [_leaf("a")], "edges": [_edge("a", "F")]}, live)
    found = _one(report, "external_target_invalid")
    assert found.edges == (("a", "F", "blocks"),)


def test_dependency_on_ancestor():
    report = _check({
        "tasks": [_container("C"), _leaf("a", parent_id="C")],
        "edges": [_edge("a", "C", "waits-for")],
    })
    found = _one(report, "dependency_on_ancestor")
    assert found.tasks == ("a", "C")
    assert not _findings(report, "container_endpoint")


def test_post_landing_inside_delivery_epic():
    report = _check({"tasks": [
        _container("E"),
        _leaf("a", parent_id="E"),
        _leaf("r", parent_id="E", placement="operational_root"),
    ]})
    assert _one(report, "post_landing_inside_delivery_epic").tasks == ("r", "E")


def test_delivery_targets_mark_post_landing_work():
    report = _check({"tasks": [
        _container("E"),
        _leaf("a", parent_id="E"),
        _leaf("r", parent_id="E", delivery_targets=["a"]),
    ]})
    assert _one(report, "post_landing_inside_delivery_epic").tasks == ("r", "E")


def test_delivery_condition_cycle_through_the_target_subtree():
    # r waits on the delivery of epic T; T's leaf t1 waits on r.  The
    # structural graph has no cycle, but T only lands once t1 has.
    report = _check({
        "tasks": [
            _container("T"),
            _leaf("t1", parent_id="T"),
            _leaf("r", placement="operational_root", delivery_targets=["T"]),
        ],
        "edges": [_edge("t1", "r")],
    })
    found = _one(report, "delivery_condition_cycle")
    assert set(found.tasks) == {"T", "r", "t1"}
    assert ("r", "T", "delivery-of") in found.edges
    assert not _findings(report, "cycle")


def test_mixed_blocks_and_delivery_of_cycle():
    report = _check({
        "tasks": [_leaf("a"), _leaf("r", placement="operational_root")],
        "edges": [_edge("r", "a", "delivery-of"), _edge("a", "r")],
    })
    found = _one(report, "delivery_condition_cycle")
    assert set(found.edges) == {("r", "a", "delivery-of"), ("a", "r", "blocks")}
    assert not _findings(report, "cycle")


def test_delivery_condition_cycle_through_a_live_delivery_edge():
    live = _live(
        tasks={"R": {"status": "DEFINED", "title": "Root"},
               "T": {"status": "DEFINED", "title": "Target"}},
        edges={("R", "T", "delivery-of")},
        members={"R", "T"},
    )
    report = _check({"tasks": [], "edges": [_edge("T", "R")]}, live)
    assert set(_one(report, "delivery_condition_cycle").tasks) == {"R", "T"}


def test_delivery_of_without_a_loop_passes():
    report = _check({
        "tasks": [_container("T"), _leaf("t1", parent_id="T"),
                  _leaf("r", placement="operational_root", delivery_targets=["T"])],
    })
    assert report.passed, report.to_dict()


def test_section_anchor_missing_on_any_member():
    report = _check({"tasks": [_container("E", section_anchor=" "),
                               _leaf("a", parent_id="E")]})
    assert _one(report, "section_anchor_missing").tasks == ("E",)


def test_primary_outcome_missing_on_a_leaf_only():
    report = _check({"tasks": [_container("E"), _leaf("a", parent_id="E",
                                                      primary_outcome=None)]})
    assert _one(report, "primary_outcome_missing").tasks == ("a",)


def test_acceptance_missing_on_a_leaf():
    report = _check({"tasks": [_leaf("a", acceptance=["", None])]})
    assert _one(report, "acceptance_missing").tasks == ("a",)


def test_profile_pinned_on_a_task_and_an_edit():
    live = _live(tasks={"M": {"status": "DEFINED", "title": "M"}}, members={"M"})
    report = _check({
        "tasks": [_leaf("a", profile_id="worker-x"), _leaf("b", model=None)],
        "edits": [{"task_id": "M", "harness": "codex"}],
    }, live)
    found = _findings(report, "profile_pinned")
    assert [(f.tasks, f.data["keys"]) for f in found] == [
        (("a",), ["profile_id"]), (("M",), ["harness"]),
    ]


# ---------------------------------------------------------------------------
# Retryable hard codes
# ---------------------------------------------------------------------------


def test_stale_revision_is_retryable():
    report = _check({"tasks": [_leaf("a")]}, _live(revision=3), expected_revision=2)
    found = _one(report, "stale_revision")
    assert found.retryable and found.data == {"expected": 2, "current": 3}
    assert not report.passed and report.retryable


def test_matching_revision_passes():
    assert _check({"tasks": [_leaf("a")]}, _live(revision=3), expected_revision=3).passed


def test_active_claim_on_a_controlled_member():
    live = _live(tasks={"M": {"status": "IN_PROGRESS", "title": "M"}},
                 members={"M"}, claimed={"M"})
    report = _check({"tasks": [_leaf("a")], "edits": [{"task_id": "M", "title": "x"}]}, live)
    assert _one(report, "active_claim").tasks == ("M",)
    assert report.retryable


def test_integration_owned():
    owner = {"operation_id": "op1", "state": "running", "task_id": "M", "role": "parent"}
    live = _live(tasks={"M": {"status": "DEFINED", "title": "M"}}, members={"M"},
                 integration_owner=owner)
    report = _check({"tasks": [_leaf("a")]}, live)
    found = _one(report, "integration_owned")
    assert found.tasks == ("M",) and found.data["operation_id"] == "op1"
    assert report.retryable


def test_a_structural_failure_makes_a_retryable_report_final():
    report = _check({"tasks": [_leaf("a", acceptance=None)]}, _live(revision=3),
                    expected_revision=2)
    assert {"stale_revision", "acceptance_missing"} <= set(report.codes)
    assert not report.retryable


def test_raise_for_hard_carries_the_first_code():
    report = _check({"tasks": [_leaf("a")], "edges": [_edge("a", "ghost")]})
    with pytest.raises(BatchCheckFailed) as caught:
        report.raise_for_hard()
    assert caught.value.code == "external_target_invalid"
    assert caught.value.retryable is False
    assert caught.value.report is report
    _check({"tasks": [_leaf("a")]}).raise_for_hard()


# ---------------------------------------------------------------------------
# Informational edges never enter the structural checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["related", "discovered-from", "duplicates", "supersedes"])
def test_informational_edges_form_no_cycle_endpoint_or_ancestor_finding(kind):
    report = _check({
        "tasks": [_container("C"), _leaf("a", parent_id="C"), _leaf("b")],
        "edges": [_edge("a", "b", kind), _edge("b", "a", kind), _edge("b", "C", kind),
                  _edge("a", "C", kind)],
    })
    assert report.passed, report.to_dict()
    assert not {"redundant_edge", "unexplained_serialization"} & set(report.codes)


# ---------------------------------------------------------------------------
# Advisory findings
# ---------------------------------------------------------------------------


def test_large_leaf_and_the_sizeless_gap():
    report = _check({"tasks": [_leaf("a", size="l"), _leaf("b", size=None)]})
    assert report.passed
    found = _one(report, "large_leaf")
    assert found.tasks == ("a",) and found.severity == "advisory"
    assert [(g.code, g.tasks) for g in report.gaps] == [("large_leaf", ("b",))]


def test_mixed_outcomes():
    report = _check({"tasks": [
        _leaf("a", primary_outcome=["fast reads", "new CLI"]),
        _leaf("b", section_anchor=["§2", "§2"]),
    ]})
    found = _one(report, "mixed_outcomes")
    assert found.tasks == ("a",)
    assert found.data == {"primary_outcome": ["fast reads", "new CLI"]}


@pytest.mark.parametrize("command,broad", [
    ("aq test tests/", True),
    ("pytest", True),
    ("python -m pytest -x", True),
    ("aq test tests/test_x.py", False),
    ("pytest tests/test_x.py::test_one", False),
    ("aq test tests/ -k schema", False),
    ("pytest --lf", False),
    ("make check", False),
    ("ruff check src", False),
])
def test_broad_command(command, broad):
    report = _check({"tasks": [_leaf("a", commands=[command])]})
    assert bool(_findings(report, "broad_command")) is broad


def test_broad_command_in_backticked_acceptance():
    report = _check({"tasks": [_leaf("a", acceptance="`aq test tests/` stays green")]})
    assert _one(report, "broad_command").data == {"command": "aq test tests/"}


def test_duplicate_title_in_the_batch_and_against_open_tasks():
    live = _live(tasks={
        "OPEN": {"status": "READY", "title": "Ship  the reader"},
        "DONE": {"status": "COMPLETED", "title": "Ship the reader"},
    })
    report = _check({"tasks": [_leaf("a", title="Ship the Reader"),
                               _leaf("b", title="ship the reader")]}, live)
    assert _one(report, "duplicate_title").tasks == ("a", "b", "OPEN")


def test_redundant_edge_declared_twice():
    report = _check({"tasks": [_leaf("a"), _leaf("b")],
                     "edges": [_edge("a", "b"), _edge("a", "b")]})
    assert _one(report, "redundant_edge").edges == (("a", "b", "blocks"),)


def test_redundant_edge_already_live():
    live = _live(tasks={"X": {"status": "READY", "title": "X"}}, edges={("M", "X", "blocks")},
                 members={"M"})
    live.tasks["M"] = {"status": "DEFINED", "title": "M"}
    report = _check({"tasks": [], "edges": [_edge("M", "X")]}, live)
    assert "declared twice" in _one(report, "redundant_edge").detail


def test_redundant_edge_implied_by_a_longer_path():
    report = _check({"tasks": [_leaf("a"), _leaf("b"), _leaf("c")],
                     "edges": [_edge("a", "b"), _edge("b", "c"), _edge("a", "c")]})
    found = _one(report, "redundant_edge")
    assert found.edges == (("a", "c", "blocks"),)
    assert "longer path" in found.detail


def test_unexplained_serialization():
    report = _check({"tasks": [_leaf("a"), _leaf("b"), _leaf("c")],
                     "edges": [_edge("a", "b", reason=None), _edge("c", "b"),
                               _edge("a", "c", "waits-for", reason=None)]})
    assert _one(report, "unexplained_serialization").edges == (("a", "b", "blocks"),)


def test_concentrated_ownership_and_the_undeclared_gap():
    report = _check({"tasks": [
        _leaf("a", owned_modules=["src/x.py", "src/a.py"]),
        _leaf("b", owned_modules=["src/x.py"]),
        _leaf("c", owned_modules=None),
        _leaf("d", owned_modules=[]),
    ]})
    found = _one(report, "concentrated_ownership")
    assert found.tasks == ("a", "b") and found.data == {"module": "src/x.py"}
    assert [(g.code, g.tasks) for g in report.gaps] == [("concentrated_ownership", ("c",))]


def test_advisories_never_refuse():
    report = _check({"tasks": [_leaf("a", size="l", title="Same"), _leaf("b", title="Same",
                                                                         owned_modules=["src/a.py"])],
                     "edges": [_edge("a", "b", reason=None)]})
    assert report.passed and report.hard == [] and len(report.advisory) >= 3
    assert {f["severity"] for f in report.to_dict()["advisory"]} == {"advisory"}


# ---------------------------------------------------------------------------
# Stored member rows
# ---------------------------------------------------------------------------


def test_from_members_reads_roles_and_skips_live_duplicate_edges():
    members = [
        {"task_id": "E", "role": "container", "section_anchor": "§E", "title": "Epic"},
        {"task_id": "a", "role": "leaf", "parent_task_id": "E", "section_anchor": "§a",
         "primary_outcome": "a lands", "acceptance": "`aq test tests/test_a.py`",
         "size": "s", "owned_modules": ["src/a.py"], "title": "A"},
        {"task_id": "r", "role": "operational_root", "section_anchor": "§r",
         "primary_outcome": "r runs", "acceptance": "`aq test tests/test_r.py`",
         "size": "s", "owned_modules": ["src/r.py"], "title": "R",
         "delivery_targets": ["E"]},
        {"task_id": "gone", "role": "leaf", "state": "retired", "section_anchor": "§g",
         "primary_outcome": "x"},
    ]
    stored_edges = [{"task_id": "a", "depends_on_task_id": "E", "dep_type": "parent-child"}]
    graph = BatchGraph.from_members(members, stored_edges)
    assert graph.stored and set(graph.nodes) == {"E", "a", "r"}
    assert graph.nodes["r"].placement == "operational_root"
    assert ("r", "E", "delivery-of") in {e.key for e in graph.edges}
    live = _live(
        tasks={tid: {"status": "DEFINED", "title": n.title, "parent_task_id":
                     ("E" if tid == "a" else None)} for tid, n in graph.nodes.items()},
        edges={("a", "E", "parent-child"), ("r", "E", "delivery-of")},
        members=set(graph.nodes),
    )
    report = check(graph, live, authority=AUTH)
    assert report.passed and report.findings == (), report.to_dict()


# ---------------------------------------------------------------------------
# The task_batch_validate command
# ---------------------------------------------------------------------------


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    yield h
    if getattr(h, "_db", None) is not None:
        await h._db.close()


async def test_validate_command_reports_a_clean_change_set(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    result = await handler.execute("task_batch_validate", {
        "project_id": "p1",
        "tasks": [_container("E"), _leaf("a", parent_id="E")],
    })
    assert result["success"] is True, result
    assert result["passed"] is True and result["hard"] == [] and result["gaps"] == []
    assert result["batch_id"] is None


async def test_validate_command_reads_the_live_graph(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    await handler.execute("create_project", {"id": "p2", "name": "p2"})
    here = await handler.execute("create_task", {"project_id": "p1", "title": "Ship the reader",
                                                 "description": ""})
    there = await handler.execute("create_task", {"project_id": "p2", "title": "Elsewhere",
                                                  "description": ""})
    result = await handler.execute("task_batch_validate", {
        "project_id": "p1",
        "tasks": [_leaf("a", title="ship the reader")],
        "edges": [_edge("a", there["created"]), _edge("a", "ghost")],
    })
    assert result["success"] is True, result
    assert result["passed"] is False and result["retryable"] is False
    assert {f["code"] for f in result["hard"]} == {"cross_project", "external_target_invalid"}
    duplicate = [f for f in result["advisory"] if f["code"] == "duplicate_title"]
    assert duplicate and duplicate[0]["tasks"] == ["a", here["created"]]


async def test_validate_command_checks_a_stored_batch_revision(handler):
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    proposal = await handler.execute("task_batch_propose", {
        "project_id": "p1", "source": "spec:doc",
        "tasks": [{"tempId": "a", "title": "A", "description": ""}],
    })
    assert proposal["success"] is True, proposal
    now = time.time()
    async with handler.db._engine.begin() as conn:
        await conn.execute(ingest_batches.insert().values(
            batch_id=proposal["proposal_id"], project_id="p1", source_review_id="rev-1",
            source_revision=1, source_content_sha256="s", decision_id="d1",
            lineage_kind="none", idempotency_key="k1", structure_revision=4,
            structure_digest="sha256:x", created_at=now, updated_at=now,
        ))
    base = {"project_id": "p1", "batch_id": proposal["proposal_id"], "tasks": [_leaf("b")]}

    stale = await handler.execute("task_batch_validate", {
        **base, "source": "spec:doc", "expected_revision": 3,
    })
    assert stale["success"] is True, stale
    assert [f["code"] for f in stale["hard"]] == ["stale_revision"]
    assert stale["retryable"] is True

    current = await handler.execute("task_batch_validate", {
        **base, "source": "spec:doc", "expected_revision": 4,
    })
    assert current["passed"] is True, current

    foreign = await handler.execute("task_batch_validate", {
        **base, "source": "spec:other", "expected_revision": 4,
    })
    assert [f["code"] for f in foreign["hard"]] == ["authority"]


async def test_validate_command_refuses_unknown_project_and_batch(handler):
    missing = await handler.execute("task_batch_validate", {"project_id": "nope"})
    assert missing["success"] is False and "not found" in missing["error"]
    await handler.execute("create_project", {"id": "p1", "name": "p1"})
    unknown = await handler.execute("task_batch_validate", {"project_id": "p1",
                                                            "batch_id": "batch-ghost"})
    assert unknown["success"] is False and "batch-ghost" in unknown["error"]
