"""Regression coverage for installing compiled playbooks."""

from types import SimpleNamespace

import pytest

from src.playbooks.manager import PlaybookManager
from src.playbooks.models import CompiledPlaybook, PlaybookNode
from src.playbooks.store import CompiledPlaybookStore


def _playbook(
    playbook_id: str, version: int, trigger: str, scope: str = "system"
) -> CompiledPlaybook:
    return CompiledPlaybook(
        id=playbook_id,
        version=version,
        source_hash=f"hash-{version}",
        triggers=[trigger],
        scope=scope,
        nodes={"done": PlaybookNode(entry=True, terminal=True)},
    )


@pytest.mark.asyncio
async def test_install_compiled_swaps_active_and_reindexes_triggers(tmp_path):
    store = CompiledPlaybookStore(SimpleNamespace(compiled_root=str(tmp_path)))
    manager = PlaybookManager(config=None, store=store)
    await manager.install_compiled(_playbook("pb", 1, "task.created"))
    await manager.install_compiled(_playbook("pb", 2, "task.completed"))
    assert manager.get_playbooks_by_trigger("task.created") == []
    assert manager.get_playbooks_by_trigger("task.completed")[0].version == 2
    assert manager.active_playbooks["pb"].version == 2


@pytest.mark.asyncio
async def test_install_compiled_persists_with_correct_scope_partition(tmp_path):
    store = CompiledPlaybookStore(SimpleNamespace(compiled_root=str(tmp_path)))
    manager = PlaybookManager(config=None, store=store)
    system = _playbook("system", 1, "task.created")
    project = _playbook("project", 1, "task.created", "project")
    agent = _playbook("agent", 1, "task.created", "agent-type:coding")
    manager.set_scope_identifier("project", "project-1")
    await manager.install_compiled(system)
    await manager.install_compiled(project)
    await manager.install_compiled(agent)
    assert store.load("system", "system") is not None
    assert store.load("project", "project", "project-1") is not None
    assert store.load("agent", "agent_type", "coding") is not None


@pytest.mark.asyncio
async def test_compile_task_project_id_is_deterministic_across_row_order():
    first = SimpleNamespace(id="z-project")
    second = SimpleNamespace(id="a-project")
    for projects in ([first, second], [second, first]):
        manager = PlaybookManager(
            config=None,
            command_handler=SimpleNamespace(
                db=SimpleNamespace(list_projects=lambda: _projects(projects))
            ),
        )
        assert await manager.compile_task_project_id("system", None) == "a-project"


async def _projects(projects):
    return projects
