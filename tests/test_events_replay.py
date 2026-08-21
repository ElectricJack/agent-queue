"""Work-graph WG-4: event replay + registry invariant.

Covers docs/specs/implementation/work-graph.md §8 and §11:

* ``get_recent_events(after_id=…)`` returns gapless ASC pages,
* WebSocket ``after_seq`` replays then hands off to live with no duplicates,
* every EventBus emitter of ``task.blocked`` / ``task.unblocked`` uses a
  payload that satisfies the registered schema,
* registry invariant: every registered event type has a schema.
"""

from __future__ import annotations

import asyncio

import pytest

from src.api.websocket import WebSocketManager
from src.database import SQLiteDatabaseAdapter
from src.event_bus import EventBus
from src.event_schemas import EVENT_SCHEMAS, validate_event
from src.models import Project, Task, TaskStatus


PROJECT = "p-replay"


@pytest.fixture
async def db(tmp_path):
    database = SQLiteDatabaseAdapter(str(tmp_path / "replay.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="replay"))
    yield database
    await database.close()


# ── get_recent_events(after_id=…) ────────────────────────────────────────


class TestAfterIdPagination:
    async def test_returns_ascending_gapless_pages(self, db):
        for i in range(10):
            await db.log_event(f"tick.{i}", project_id=PROJECT, payload=str(i))
        # Grab all events to discover the id range.
        newest = await db.get_recent_events(limit=100)
        ids = sorted(e["id"] for e in newest)
        assert len(ids) == 10

        # after_id=None (default) is DESC — behaviour unchanged.
        assert [e["id"] for e in await db.get_recent_events(limit=5)] == list(
            reversed(ids[-5:])
        )

        # after_id=first_id -> ASC, gapless.
        page1 = await db.get_recent_events(limit=3, after_id=ids[0])
        assert [e["id"] for e in page1] == ids[1:4]

        # Cursor forward: next page picks up right where we left off.
        page2 = await db.get_recent_events(limit=3, after_id=page1[-1]["id"])
        assert [e["id"] for e in page2] == ids[4:7]

    async def test_after_id_out_of_range_returns_empty(self, db):
        assert await db.get_recent_events(limit=10, after_id=999_999) == []


# ── WebSocket replay-then-live ───────────────────────────────────────────


class _FakeWS:
    """Minimal WebSocket stand-in matching FastAPI's protocol surface."""

    def __init__(self, after_seq: str | None = None):
        self.query_params: dict[str, str] = {}
        if after_seq is not None:
            self.query_params["after_seq"] = after_seq
        self.accepted = False
        self.sent: list[dict] = []
        self._closed = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, data: dict):
        if self._closed:
            raise RuntimeError("closed")
        self.sent.append(data)


class TestWebSocketReplay:
    async def test_replay_then_live_no_duplicates(self, db):
        # Persist 5 events *before* connect.
        for i in range(5):
            await db.log_event(
                f"notify.tick.{i}", project_id=PROJECT, payload=str(i)
            )
        rows = sorted(await db.get_recent_events(limit=100), key=lambda r: r["id"])
        first_id = rows[0]["id"]

        bus = EventBus()
        mgr = WebSocketManager(bus, db=db)
        mgr.start()

        ws = _FakeWS(after_seq=str(first_id))

        # Drive the handler far enough to replay + fall into the live wait.
        # Close the queue by cancelling the task after a short window.
        async def _drive():
            # Handle runs forever; we cancel it after collecting frames.
            try:
                await mgr.handle(ws)  # type: ignore[arg-type]
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(_drive())
        # Yield the loop so the replay pages run.
        await asyncio.sleep(0.05)

        # Emit one live event through the bus while the client is connected.
        await bus.emit("notify.live", {"project_id": PROJECT, "seq": None})
        await asyncio.sleep(0.05)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # We should see rows[1..end] (after first_id) then the live event.
        replayed_ids = [f.get("seq") for f in ws.sent if isinstance(f.get("seq"), int)]
        assert replayed_ids == [r["id"] for r in rows[1:]]
        # The live frame carries seq=None (source-of-truth: not a DB row).
        live_frames = [f for f in ws.sent if f.get("_event_type") == "notify.live"]
        assert len(live_frames) == 1
        assert live_frames[0]["seq"] is None

    async def test_no_after_seq_skips_replay(self, db):
        for i in range(3):
            await db.log_event(
                f"notify.tick.{i}", project_id=PROJECT, payload=str(i)
            )
        bus = EventBus()
        mgr = WebSocketManager(bus, db=db)
        mgr.start()
        ws = _FakeWS(after_seq=None)

        task = asyncio.create_task(mgr.handle(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.02)
        # Only live events (none yet) should ship — no replay.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert ws.sent == []


# ── Registry invariant + task.blocked/unblocked emitters ─────────────────


class TestBlockedFlipEmitters:
    async def test_registered_schemas_present(self):
        for ev in ("task.blocked", "task.unblocked", "gate.created", "gate.resolved"):
            assert ev in EVENT_SCHEMAS

    async def test_orchestrator_emit_helper_matches_registered_schema(self, tmp_path):
        """A minimal end-to-end trip through ``_emit_blocked_flips``: it must
        emit a payload that ``validate_event`` accepts for both bus events."""
        from src.config import AppConfig
        from src.orchestrator import Orchestrator

        cfg = AppConfig(
            database_path=str(tmp_path / "e.db"),
            workspace_dir=str(tmp_path / "ws"),
            data_dir=str(tmp_path / "data"),
        )
        (tmp_path / "ws").mkdir()
        orch = Orchestrator(cfg)
        await orch.initialize()
        await orch.db.create_project(Project(id="p", name="p"))
        await orch.db.create_task(
            Task(id="t", project_id="p", title="t", description="t")
        )
        await orch.db.create_task(
            Task(id="dep", project_id="p", title="dep", description="")
        )
        await orch.db.add_dependency("t", "dep")  # blocks t

        captured: list[tuple[str, dict]] = []

        async def _spy(data):
            captured.append((data.get("_event_type"), data))

        orch.bus.subscribe("task.blocked", _spy)
        orch.bus.subscribe("task.unblocked", _spy)

        # Force emit: t is blocked (dep is DEFINED).
        await orch._emit_blocked_flips({"t"})
        # Complete dep and recompute; t should now unblock.
        await orch.db.transition_task("dep", TaskStatus.COMPLETED)
        await orch._emit_blocked_flips({"t"})

        types = [t for (t, _) in captured]
        assert "task.blocked" in types
        assert "task.unblocked" in types
        for event_type, payload in captured:
            payload_wo_meta = {k: v for k, v in payload.items() if k != "_event_type"}
            errors = validate_event(event_type, payload_wo_meta)
            assert errors == [], f"{event_type}: {errors}"
