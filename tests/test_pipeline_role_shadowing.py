"""Tests for role shadowing of pipeline playbooks.

Spec §4.5: when a project-scoped pipeline playbook shares its ``role`` with a
system-scoped pipeline playbook, the project version wins and the system one is
dropped from the candidate list for that event.  Non-pipeline playbooks are
never shadowed.

Cross-project isolation: a project-A pipeline must NOT suppress the system
default for a project-B event.
"""

from src.playbooks.manager import PlaybookManager


def _fake(id: str, scope: str, role: str, kind: str = "pipeline"):
    from types import SimpleNamespace

    return SimpleNamespace(id=id, scope=scope, kind=kind, role=role)


def _mgr_with_scope_identifiers(mapping: dict) -> PlaybookManager:
    """Create a bare PlaybookManager instance with _scope_identifiers pre-set."""
    mgr = PlaybookManager.__new__(PlaybookManager)  # bypass __init__
    mgr._scope_identifiers = mapping
    return mgr


def test_project_pipeline_shadows_system():
    sys_pb = _fake("sys-default-pipeline", "system", "default-pipeline")
    proj_pb = _fake("proj-default-pipeline", "project", "default-pipeline")
    mgr = _mgr_with_scope_identifiers({"proj-default-pipeline": "proj-A"})
    kept = mgr._select_after_shadowing([sys_pb, proj_pb], event={"project_id": "proj-A"})
    assert [pb.id for pb in kept] == ["proj-default-pipeline"]


def test_no_shadow_when_roles_differ():
    sys_pb = _fake("s", "system", "default-pipeline")
    proj_pb = _fake("p", "project", "review-pipeline")
    mgr = _mgr_with_scope_identifiers({"p": "proj-A"})
    kept = mgr._select_after_shadowing([sys_pb, proj_pb], event={"project_id": "proj-A"})
    assert {pb.id for pb in kept} == {"s", "p"}


def test_non_pipeline_playbooks_never_shadow():
    sys_pb = _fake("s", "system", "default-pipeline", kind="llm")
    proj_pb = _fake("p", "project", "default-pipeline", kind="pipeline")
    mgr = _mgr_with_scope_identifiers({"p": "proj-A"})
    kept = mgr._select_after_shadowing([sys_pb, proj_pb], event={"project_id": "proj-A"})
    assert {pb.id for pb in kept} == {"s", "p"}


# ---------------------------------------------------------------------------
# Cross-project isolation regression tests (fix round 1)
# ---------------------------------------------------------------------------


def test_cross_project_no_bleed_system_pipeline_kept_for_other_project():
    """Project-A pipeline must NOT shadow system pipeline for a project-B event."""
    sys_pb = _fake("sys-default", "system", "default-pipeline")
    proj_a_pb = _fake("proj-a-default", "project", "default-pipeline")
    # _scope_identifiers maps proj-a-default → proj-A
    mgr = _mgr_with_scope_identifiers({"proj-a-default": "proj-A"})
    # Event belongs to project-B
    kept = mgr._select_after_shadowing([sys_pb, proj_a_pb], event={"project_id": "proj-B"})
    # System pipeline must survive because proj-A's pipeline doesn't own proj-B
    assert "sys-default" in {pb.id for pb in kept}


def test_cross_project_correct_project_pipeline_shadows_system():
    """Project-A pipeline MUST shadow system pipeline for a project-A event."""
    sys_pb = _fake("sys-default", "system", "default-pipeline")
    proj_a_pb = _fake("proj-a-default", "project", "default-pipeline")
    mgr = _mgr_with_scope_identifiers({"proj-a-default": "proj-A"})
    # Event belongs to project-A
    kept = mgr._select_after_shadowing([sys_pb, proj_a_pb], event={"project_id": "proj-A"})
    kept_ids = {pb.id for pb in kept}
    assert "proj-a-default" in kept_ids
    assert "sys-default" not in kept_ids


def test_two_project_pipelines_only_matching_project_shadows():
    """Both proj-A and proj-B pipelines present; only the matching one shadows."""
    sys_pb = _fake("sys-default", "system", "default-pipeline")
    proj_a_pb = _fake("proj-a-pipe", "project", "default-pipeline")
    proj_b_pb = _fake("proj-b-pipe", "project", "default-pipeline")
    mgr = _mgr_with_scope_identifiers({"proj-a-pipe": "proj-A", "proj-b-pipe": "proj-B"})
    # Event for proj-A: proj-A pipeline shadows system; proj-B pipeline is kept (scope filter
    # in _matches_scope handles dropping it later, but _select_after_shadowing must not
    # use proj-B to suppress the system pipeline for proj-A — here both project pipelines
    # ARE in candidates, and proj-A's pipeline should shadow the system one).
    kept = mgr._select_after_shadowing(
        [sys_pb, proj_a_pb, proj_b_pb], event={"project_id": "proj-A"}
    )
    kept_ids = {pb.id for pb in kept}
    # System suppressed (proj-A pipeline claims the role for proj-A)
    assert "sys-default" not in kept_ids
    assert "proj-a-pipe" in kept_ids
    assert "proj-b-pipe" in kept_ids
