from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.commands.playbook_commands import PlaybookCommandsMixin


class _Handler(PlaybookCommandsMixin):
    def __init__(self) -> None:
        self.db = SimpleNamespace(
            list_playbook_activations=AsyncMock(return_value=[{
                "activation_id": "activation-1",
                "playbook_id": "router",
                "scope": "system",
                "scope_identifier": "",
                "active_artifact_sha256": "sha256:artifact",
                "enabled": True,
                "health": "ready",
            }])
        )
        artifact = SimpleNamespace(
            id="router",
            version=2,
            compiled_at=datetime(2026, 9, 4, tzinfo=UTC),
            rules=[SimpleNamespace(trigger=SimpleNamespace(event_type="assignment.route.requested"))],
            steps={"choose": object(), "done": object()},
        )
        self.engine = SimpleNamespace(
            services=SimpleNamespace(
                artifact_store=SimpleNamespace(load=lambda _sha: artifact)
            )
        )

    def _v2_engine(self):
        return self.engine


async def test_list_playbooks_projects_v2_activations_to_dashboard_summaries() -> None:
    result = await _Handler()._cmd_list_playbooks({})

    assert result == {
        "playbooks": [{
            "id": "router",
            "scope": "system",
            "scope_identifier": "",
            "triggers": ["assignment.route.requested"],
            "version": 2,
            "compiled_at": "2026-09-04T00:00:00+00:00",
            "node_count": 2,
            "status": "ready",
            "enabled": True,
        }],
        "count": 1,
    }


async def test_list_playbooks_applies_scope_filter() -> None:
    result = await _Handler()._cmd_list_playbooks({"scope": "project"})

    assert result == {"playbooks": [], "count": 0}
