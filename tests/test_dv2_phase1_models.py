from src.models import AgentProfile, Task


def test_task_has_dedup_and_intelligence_class_optional():
    t = Task(id="t1", project_id="p", title="x", description="y")
    assert t.dedup_key is None
    assert t.intelligence_class is None


def test_agent_profile_defaults():
    p = AgentProfile(id="q", name="Q")
    assert p.default_class == ""
    assert p.needs_workspace is True
