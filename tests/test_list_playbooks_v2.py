from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.commands.playbook_commands import PlaybookCommandsMixin


class _Handler(PlaybookCommandsMixin):
    def __init__(self, tmp_path=None) -> None:
        self.db = SimpleNamespace(
            list_playbook_activations=AsyncMock(return_value=[{
                "activation_id": "activation-1",
                "playbook_id": "router",
                "scope": "system",
                "scope_identifier": "",
                "active_artifact_sha256": "sha256:artifact",
                "enabled": True,
                "health": "ready",
            }]),
            list_runs=AsyncMock(return_value=[]),
        )
        self.config = SimpleNamespace(data_dir=str(tmp_path) if tmp_path else "/tmp/aq-test")
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


async def test_list_runs_projects_v2_snapshots_to_dashboard_summaries() -> None:
    handler = _Handler()
    handler.db.list_runs.return_value = [SimpleNamespace(
        run_id="run-1",
        playbook_id="router",
        artifact_sha256="sha256:artifact",
        lifecycle=SimpleNamespace(value="completed"),
        current_step_id="done",
        budget=SimpleNamespace(total_tokens=42),
        started_at=10.0,
        completed_at=12.5,
        error=None,
    )]

    result = await handler._cmd_list_playbook_runs({"playbook_id": "router", "limit": 20})

    assert result["runs"][0] == {
        "run_id": "run-1",
        "playbook_id": "router",
        "playbook_version": 2,
        "status": "completed",
        "current_node": "done",
        "tokens_used": 42,
        "started_at": 10.0,
        "completed_at": 12.5,
        "duration_seconds": 2.5,
        "error": None,
    }


async def test_get_source_reads_the_active_v2_scope(tmp_path) -> None:
    handler = _Handler(tmp_path)
    source = tmp_path / "vault" / "system" / "playbooks" / "router.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Router\n", encoding="utf-8")

    result = await handler._cmd_get_playbook_source({"playbook_id": "router"})

    assert result["playbook_id"] == "router"
    assert result["path"] == str(source.resolve())
    assert result["markdown"] == "# Router\n"
    assert result["source_hash"].startswith("sha256:")
