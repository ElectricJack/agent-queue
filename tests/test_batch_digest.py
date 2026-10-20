"""Review identity covers live graph semantics and survives cosmetic edits."""

from copy import deepcopy

import pytest

from src.task_graph.batch_digest import canonical_batch_structure, compute_structure_digest


@pytest.fixture
def graph():
    members = [
        {
            "task_id": "epic.1", "member_key": "code", "state": "active", "role": "leaf",
            "parent_task_id": "epic", "title": "Persist holds", "description": "Must persist holds",
            "acceptance": ["Upgrade preserves work"], "constraints": ["PostgreSQL only"],
            "section_anchor": "Schema", "primary_outcome": "Persist holds", "size": "m",
        },
        {
            "task_id": "epic", "member_key": "schema", "state": "active", "role": "container",
            "parent_task_id": None, "title": "Schema", "description": "Deliver schema",
            "acceptance": [], "constraints": [], "section_anchor": "Schema",
            "primary_outcome": "Schema delivery", "size": "m",
        },
    ]
    edges = [
        {"task_id": "epic.1", "depends_on_task_id": "epic", "dep_type": "parent-child"},
        {"task_id": "epic.1", "depends_on_task_id": "external", "dep_type": "blocks"},
    ]
    return members, edges


def test_digest_is_order_independent_and_uses_stable_member_keys(graph):
    members, edges = graph
    digest = compute_structure_digest(members, edges)
    assert len(digest) == 64
    assert digest == compute_structure_digest(reversed(members), reversed(edges))
    renamed_members, renamed_edges = deepcopy(graph)
    names = {"epic": "batch-root", "epic.1": "batch-leaf"}
    for member in renamed_members:
        member["task_id"] = names[member["task_id"]]
        member["parent_task_id"] = names.get(member["parent_task_id"])
    for edge in renamed_edges:
        edge["task_id"] = names[edge["task_id"]]
        edge["depends_on_task_id"] = names.get(edge["depends_on_task_id"], edge["depends_on_task_id"])
    assert compute_structure_digest(renamed_members, renamed_edges) == digest


@pytest.mark.parametrize("field,value", [
    ("member_key", "different_key"), ("role", "operational_root"),
    ("parent_task_id", "external_parent"), ("description", "May persist holds"),
    ("acceptance", ["Upgrade may lose work"]), ("constraints", []),
    ("section_anchor", "Different section"), ("primary_outcome", "Different outcome"),
    ("size", "l"), ("state", "retired"),
])
def test_every_structural_member_field_invalidates_digest(graph, field, value):
    members, edges = graph
    digest = compute_structure_digest(members, edges)
    members[0][field] = value
    assert compute_structure_digest(members, edges) != digest


def test_cosmetic_allowlist_preserves_receipt_identity_and_readable_title(graph):
    members, edges = graph
    digest = compute_structure_digest(members, edges)
    members[0].update(title="New label", priority=20, tags=["renamed"])
    members[0]["description"] = "  Must\n\tpersist   holds  "
    assert compute_structure_digest(members, edges) == digest
    snapshot = canonical_batch_structure(members, edges)
    assert snapshot["members"][0]["title"] == "New label"
    assert snapshot["members"][0]["parent_key"] == {"member_key": "schema"}
    assert snapshot["members"][0]["description"] == "Must persist holds"


@pytest.mark.parametrize("field,value", [
    ("task_id", "epic"), ("depends_on_task_id", "another_external"),
    ("dep_type", "delivery-of"), ("description", "Necessary delivery condition"),
])
def test_edge_changes_including_external_targets_invalidate_digest(graph, field, value):
    members, edges = graph
    digest = compute_structure_digest(members, edges)
    edges[1][field] = value
    assert compute_structure_digest(members, edges) != digest


def test_removing_or_adding_an_incident_edge_invalidates_digest(graph):
    members, edges = graph
    digest = compute_structure_digest(members, edges)
    assert compute_structure_digest(members, edges[:1]) != digest
    edges.append({"task_id": "outside", "depends_on_task_id": "epic.1", "dep_type": "related"})
    assert compute_structure_digest(members, edges) != digest


def test_retired_history_and_unrelated_edges_do_not_change_review_identity(graph):
    members, edges = graph
    digest = compute_structure_digest(members, edges)
    members.append({"state": "retired", "task_id": "retired", "member_key": "old_key"})
    edges.append({"task_id": "outside", "depends_on_task_id": "retired", "dep_type": "blocks"})
    assert compute_structure_digest(members, edges) == digest


def test_external_ids_are_distinct_from_same_spelled_member_keys(graph):
    members, edges = graph
    internal = compute_structure_digest(members, edges)
    edges[0]["depends_on_task_id"] = "schema"  # External ID equals the root's member key.
    assert compute_structure_digest(members, edges) != internal


def test_incomplete_or_duplicate_members_cannot_be_attested(graph):
    members, edges = graph
    members.append(dict(members[0]))
    with pytest.raises(ValueError, match="unique"):
        compute_structure_digest(members, edges)
    members.pop()
    del members[0]["acceptance"]
    with pytest.raises(KeyError, match="acceptance"):
        compute_structure_digest(members, edges)
