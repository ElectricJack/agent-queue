"""``GET /api/metrics/series`` and the ``metrics.tick`` WebSocket frame.

Two seams: the route that answers a cold load or a zoom, and the fan-out
rule that decides which connected clients ever see a live sample.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.auth import LOCAL_SCOPE, RequestScope
from src.api.metrics import MAX_POINTS, build_metrics_router, choose_step
from src.api.websocket import _FORWARDED_PREFIXES, _metrics_event_allowed
from src.database import Database
from src.metrics.sampler import METRIC_TICK_EVENT

BASE = 1_700_000_000.0


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "m.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def client_factory(db):
    def _make() -> AsyncClient:
        app = FastAPI()
        app.include_router(build_metrics_router(db=db))
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")

    return _make


async def seed(db, resolution: str, count: int, step: int = 1, start: float = BASE):
    for i in range(count):
        await db.write_metrics_sample(
            resolution,
            start + i * step,
            {"agents": {"total": i}, "machine": {"load1": None}},
        )


# ---------------------------------------------------------------------------
# step selection
# ---------------------------------------------------------------------------


def test_auto_picks_the_finest_step_that_fits_the_point_budget():
    assert choose_step(0, 600, "auto") == ("1s", False)
    # A week of seconds is 604,800 points; minutes fit, so minutes it is.
    assert choose_step(0, 604_800, "auto") == ("1m", False)
    assert choose_step(0, 400 * 86_400, "auto") == ("1h", False)


def test_an_explicit_step_is_honoured_when_it_fits():
    assert choose_step(0, 3600, "1s") == ("1s", False)
    assert choose_step(0, 604_800, "1m") == ("1m", False)


def test_an_explicit_step_that_would_blow_the_budget_is_coarsened_and_flagged():
    step, truncated = choose_step(0, 30 * 86_400, "1s")
    assert step != "1s"
    # Silently returning a short window would make the chart lie about the
    # range its axis is labelled with.
    assert truncated is True


def test_auto_never_reports_truncation_because_it_chose_the_step():
    assert choose_step(0, 400 * 86_400, "auto")[1] is False


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------


async def test_empty_series_is_an_answer_not_an_error(client_factory):
    async with client_factory() as ac:
        response = await ac.get("/api/metrics/series")
    assert response.status_code == 200
    assert response.json()["samples"] == []


async def test_returns_samples_in_range_ascending(db, client_factory):
    await seed(db, "1s", 5)
    async with client_factory() as ac:
        response = await ac.get(
            "/api/metrics/series", params={"from": BASE + 1, "to": BASE + 3, "step": "1s"}
        )
    body = response.json()
    assert [row["ts"] for row in body["samples"]] == [BASE + 1, BASE + 2, BASE + 3]
    assert [row["agents"]["total"] for row in body["samples"]] == [1, 2, 3]
    assert body["step"] == "1s"


async def test_missing_readings_survive_as_null_not_zero(db, client_factory):
    await seed(db, "1s", 1)
    async with client_factory() as ac:
        response = await ac.get(
            "/api/metrics/series", params={"from": BASE, "to": BASE, "step": "1s"}
        )
    assert response.json()["samples"][0]["machine"]["load1"] is None


async def test_reads_the_resolution_it_was_asked_for(db, client_factory):
    await seed(db, "1s", 3)
    await seed(db, "1m", 3, step=60)
    async with client_factory() as ac:
        response = await ac.get(
            "/api/metrics/series", params={"from": BASE, "to": BASE + 200, "step": "1m"}
        )
    body = response.json()
    assert body["step"] == "1m"
    assert [row["ts"] for row in body["samples"]] == [BASE, BASE + 60, BASE + 120]


async def test_defaults_to_the_last_hour(db, client_factory):
    async with client_factory() as ac:
        response = await ac.get("/api/metrics/series")
    body = response.json()
    assert body["to_ts"] - body["from_ts"] == pytest.approx(3600, abs=1)


async def test_an_unknown_step_is_rejected(client_factory):
    async with client_factory() as ac:
        response = await ac.get("/api/metrics/series", params={"step": "5s"})
    assert response.status_code == 422


async def test_an_inverted_range_is_rejected(client_factory):
    async with client_factory() as ac:
        response = await ac.get(
            "/api/metrics/series", params={"from": BASE + 10, "to": BASE}
        )
    assert response.status_code == 422


async def test_hitting_the_row_limit_is_reported_as_truncation(db, client_factory, monkeypatch):
    """A window cut short by the row cap must say so, not look complete."""
    await seed(db, "1s", 6)
    monkeypatch.setattr("src.api.metrics.MAX_POINTS", 5)
    async with client_factory() as ac:
        response = await ac.get(
            "/api/metrics/series", params={"from": BASE, "to": BASE + 5, "step": "1s"}
        )
    body = response.json()
    assert body["step"] == "1s"
    assert len(body["samples"]) == 5
    assert body["truncated"] is True


def test_the_point_budget_is_a_real_number():
    assert MAX_POINTS > 0


# ---------------------------------------------------------------------------
# WebSocket fan-out
# ---------------------------------------------------------------------------


def test_metrics_frames_are_forwarded_to_websocket_clients():
    assert METRIC_TICK_EVENT.startswith(_FORWARDED_PREFIXES)


async def test_a_sampler_tick_reaches_a_local_client_and_not_a_scoped_worker():
    """End to end over the bus: sampler emit -> fan-out -> client queue."""
    import asyncio

    from src.api.websocket import WebSocketManager
    from src.event_bus import EventBus

    bus = EventBus(env="dev")
    manager = WebSocketManager(bus)
    manager._clients["dashboard"] = asyncio.Queue()
    manager._client_scope["dashboard"] = LOCAL_SCOPE
    manager._clients["worker"] = asyncio.Queue()
    manager._client_scope["worker"] = RequestScope(
        kind="session", session_id="s1", project_id="p1"
    )
    manager.start()
    try:
        await bus.emit(
            METRIC_TICK_EVENT,
            {"ts": BASE, "agents": {"total": 4}, "machine": {"load1": 3.25}},
        )
    finally:
        manager.shutdown()

    frame = manager._clients["dashboard"].get_nowait()
    assert frame["_event_type"] == METRIC_TICK_EVENT
    assert frame["agents"]["total"] == 4
    assert frame["machine"]["load1"] == 3.25
    # A fleet-wide sample names every session and profile on the box.
    assert manager._clients["worker"].empty()


def test_only_local_and_elevated_scopes_see_a_fleet_wide_sample():
    assert _metrics_event_allowed(LOCAL_SCOPE) is True
    elevated = RequestScope(
        kind="session", session_id="s1", project_id="p1", task_id=None, elevated=True
    )
    scoped = RequestScope(
        kind="session", session_id="s1", project_id="p1", task_id=None, elevated=False
    )
    assert _metrics_event_allowed(elevated) is True
    # A sample names every session and profile on the box; a scoped worker
    # has no business receiving it, and would never render it either.
    assert _metrics_event_allowed(scoped) is False
    assert _metrics_event_allowed(None) is False


def test_a_tick_is_a_registered_event_so_validation_does_not_warn():
    from src.event_schemas import validate_event

    assert validate_event(METRIC_TICK_EVENT, {"ts": BASE, "agents": {"total": 1}}) == []
    assert validate_event(METRIC_TICK_EVENT, {"agents": {}}) != []
