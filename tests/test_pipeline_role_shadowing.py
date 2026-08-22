"""Tests for role shadowing of pipeline playbooks.

Spec §4.5: when a project-scoped pipeline playbook shares its ``role`` with a
system-scoped pipeline playbook, the project version wins and the system one is
dropped from the candidate list for that event.  Non-pipeline playbooks are
never shadowed.
"""

from src.playbooks.manager import PlaybookManager


def _fake(id: str, scope: str, role: str, kind: str = "pipeline"):
    from types import SimpleNamespace

    pb = SimpleNamespace(id=id, scope=scope, kind=kind, role=role)
    pb.to_dict = lambda: {"id": id, "scope": scope, "kind": kind, "role": role}
    return pb


def test_project_pipeline_shadows_system(monkeypatch):
    sys_pb = _fake("sys-default-pipeline", "system", "default-pipeline")
    proj_pb = _fake("proj-default-pipeline", "project", "default-pipeline")
    mgr = PlaybookManager.__new__(PlaybookManager)  # bypass __init__
    kept = mgr._select_after_shadowing([sys_pb, proj_pb], event={"project_id": "p"})
    assert [pb.id for pb in kept] == ["proj-default-pipeline"]


def test_no_shadow_when_roles_differ():
    sys_pb = _fake("s", "system", "default-pipeline")
    proj_pb = _fake("p", "project", "review-pipeline")
    mgr = PlaybookManager.__new__(PlaybookManager)
    kept = mgr._select_after_shadowing([sys_pb, proj_pb], event={"project_id": "p"})
    assert {pb.id for pb in kept} == {"s", "p"}


def test_non_pipeline_playbooks_never_shadow():
    sys_pb = _fake("s", "system", "default-pipeline", kind="llm")
    proj_pb = _fake("p", "project", "default-pipeline", kind="pipeline")
    mgr = PlaybookManager.__new__(PlaybookManager)
    kept = mgr._select_after_shadowing([sys_pb, proj_pb], event={"project_id": "p"})
    assert {pb.id for pb in kept} == {"s", "p"}
