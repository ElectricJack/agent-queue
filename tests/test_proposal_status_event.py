"""``proposal.status_changed`` bus event — schema + emission.

Pane-view follow-up for the Phase 6 spec-ingestion proposal preview
(``docs/superpowers/specs/2026-08-22-pane-proposal-preview-design.md``
§8.2). Closes the live-update gap: nothing previously fired when a
proposal transitioned ``ready -> committed`` or ``ready -> discarded``.
"""
from __future__ import annotations

import pytest

from src.event_schemas import EVENT_SCHEMAS, validate_payload


class TestProposalStatusChangedSchema:
    def test_registered(self):
        assert "proposal.status_changed" in EVENT_SCHEMAS

    def test_requires_status(self):
        schema = EVENT_SCHEMAS["proposal.status_changed"]
        assert schema["required"] == ["project_id", "proposal_id", "status"]
        assert schema["optional"] == []

    def test_validate_payload_accepts_committed_and_discarded(self):
        for status in ("committed", "discarded"):
            errors = validate_payload(
                "proposal.status_changed",
                {"project_id": "p1", "proposal_id": "prop-1", "status": status},
            )
            assert errors == [], errors


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    yield h
    if hasattr(h, "_db") and h._db is not None:
        await h._db.close()


def _emitted(h) -> list[tuple[str, dict]]:
    calls = h.orchestrator.bus.emit.call_args_list
    out: list[tuple[str, dict]] = []
    for c in calls:
        args, kwargs = c
        if args:
            evt = args[0]
            payload = args[1] if len(args) > 1 else kwargs.get("payload", {})
        else:
            evt = kwargs.get("event_type") or kwargs.get("name")
            payload = kwargs.get("payload", {})
        out.append((evt, payload))
    return out


async def _propose_one_task(handler, project_id: str = "p1") -> str:
    await handler.execute("create_project", {"id": project_id, "name": project_id})
    prop = await handler.execute(
        "task_batch_propose",
        {
            "project_id": project_id,
            "source": "spec:foo",
            "tasks": [{"tempId": "a", "title": "A", "description": ""}],
            "edges": [],
        },
    )
    assert prop["success"] is True
    return prop["proposal_id"]


class TestProposalStatusChangedEmission:
    async def test_discard_emits_status_changed_event(self, handler):
        prop_id = await _propose_one_task(handler)
        r = await handler.execute("task_batch_discard", {"proposal_id": prop_id})
        assert r["success"] is True
        events = [e for e in _emitted(handler) if e[0] == "proposal.status_changed"]
        assert events and events[-1][1] == {
            "project_id": "p1",
            "proposal_id": prop_id,
            "status": "discarded",
        }

    async def test_commit_emits_status_changed_event(self, handler):
        prop_id = await _propose_one_task(handler)
        r = await handler.execute("task_batch_commit", {"proposal_id": prop_id})
        assert r["success"] is True
        events = [e for e in _emitted(handler) if e[0] == "proposal.status_changed"]
        assert events and events[-1][1] == {
            "project_id": "p1",
            "proposal_id": prop_id,
            "status": "committed",
        }

    async def test_commit_rollback_does_not_emit_status_changed(self, handler, monkeypatch):
        """A failed commit reverts the proposal to 'ready' — not a terminal
        state — and must not emit proposal.status_changed."""
        import src.commands.proposal_commands as pc

        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(pc, "_create_one_task", _boom)

        prop_id = await _propose_one_task(handler)
        r = await handler.execute("task_batch_commit", {"proposal_id": prop_id})
        assert r["success"] is False
        events = [e for e in _emitted(handler) if e[0] == "proposal.status_changed"]
        assert events == []
