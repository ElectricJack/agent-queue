"""Relay snapshots become once-only deltas, with honest gaps across restarts."""

from __future__ import annotations

import pytest

from src.config import AppConfig
from src.metrics.histogram import new_hist, observe
from src.metrics.relay import RelayReader


@pytest.fixture(autouse=True)
def _pg_backend():
    """The HTTP reader has no database."""


def cumulative(epoch: float, *http_ms: float, failures: int = 0) -> dict:
    hist = new_hist()
    for value in http_ms:
        observe(hist, value)
    return {
        "epoch": epoch, "now": epoch + 10, "http": hist, "ws_handshake": new_hist(),
        "upstream_failures": {
            "daemon_unreachable": failures, "daemon_timeout": 0,
            "daemon_bad_response": 0, "dashboard_server_stopping": 0,
        },
        "relays_open": 2,
    }


def reader(snapshots: list, enabled: bool = True) -> RelayReader:
    config = AppConfig()
    config.dashboard_server.enabled = enabled
    pending = iter(snapshots)

    def fetch(url: str, timeout: float) -> dict:
        item = next(pending)
        if isinstance(item, Exception):
            raise item
        return item

    return RelayReader(config, fetch=fetch)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "127.0.0.1", "::1"])
def test_url_follows_the_dashboard_server_section(host):
    config = AppConfig()
    config.dashboard_server.host, config.dashboard_server.port = host, 8082
    expected = "[::1]" if host == "::1" else "127.0.0.1"
    assert RelayReader(config).url() == f"http://{expected}:8082/__aq/metrics"
    config.dashboard_server.enabled = False
    assert RelayReader(config).url() is None


async def test_disabled_server_is_null_with_a_reason():
    assert await reader([], enabled=False).poll() == {
        "available": False, "reason": "dashboard_server_disabled",
    }


async def test_first_poll_is_an_epoch_reset_and_the_second_is_a_delta():
    r = reader([cumulative(1, 5), cumulative(1, 5, 50, 500, failures=1)])
    first = await r.poll()
    assert first == {"available": True, "reason": "epoch_reset", "relays_open": 2}
    second = await r.poll()
    assert second["reason"] is None
    assert second["http"]["count"] == 2 and second["http"]["max"] == 500
    assert second["http"]["sum"] == 550 and sum(second["http"]["counts"]) == 2
    assert second["upstream_failures"] == {
        "kind": "sum", "daemon_unreachable": 1, "daemon_timeout": 0,
        "daemon_bad_response": 0, "dashboard_server_stopping": 0,
    }


async def test_unchanged_snapshot_has_no_duplicate_observations():
    r = reader([cumulative(1, 5, failures=1)] * 3)
    await r.poll()
    for _ in range(2):
        out = await r.poll()
        assert out["http"]["count"] == 0
        assert out["http"]["sum"] == 0
        assert out["upstream_failures"]["daemon_unreachable"] == 0


async def test_a_server_restart_never_yields_a_negative_delta():
    r = reader([cumulative(1, 5, 50), cumulative(2, 7), cumulative(2, 7, 8)])
    await r.poll()
    assert (await r.poll())["reason"] == "epoch_reset"
    assert (await r.poll())["http"]["count"] == 1


async def test_unreachable_server_forgets_its_previous_snapshot():
    r = reader([cumulative(1, 5), OSError("down"), cumulative(1, 5, 6)])
    await r.poll()
    assert await r.poll() == {"available": False, "reason": "dashboard_server_unreachable"}
    assert (await r.poll())["reason"] == "epoch_reset"


async def test_disabled_server_forgets_its_previous_snapshot():
    r = reader([cumulative(1, 5), cumulative(1, 5, 6)])
    await r.poll()
    r.config.dashboard_server.enabled = False
    assert (await r.poll())["reason"] == "dashboard_server_disabled"
    r.config.dashboard_server.enabled = True
    assert (await r.poll())["reason"] == "epoch_reset"


@pytest.mark.parametrize("bad", [None, {}, {"epoch": 1}, cumulative(1, -1)])
async def test_malformed_snapshot_never_raises(bad):
    if isinstance(bad, dict) and "http" in bad:
        bad["http"]["counts"][0] = "bad"
    r = reader([cumulative(1, 5), bad, cumulative(1, 5, 6)])
    await r.poll()
    assert await r.poll() == {"available": False, "reason": "dashboard_server_unreachable"}
    assert (await r.poll())["reason"] == "epoch_reset"


async def test_default_fetch_reads_snapshots_over_http():
    import json
    from urllib.parse import urlsplit

    from tests.dashboard_server_helpers import serve_asgi

    snapshots = iter([cumulative(1, 5), cumulative(1, 5, 7, failures=1)])

    async def app(scope, receive, send):
        assert scope["path"] == "/__aq/metrics" and scope["method"] == "GET"
        body = json.dumps(next(snapshots)).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    async with serve_asgi(app, lifespan="off") as url:
        config = AppConfig()
        config.dashboard_server.host = urlsplit(url).hostname
        config.dashboard_server.port = urlsplit(url).port
        r = RelayReader(config)
        assert (await r.poll())["reason"] == "epoch_reset"
        result = await r.poll()
        assert result["http"]["count"] == 1 and result["http"]["sum"] == 7
        assert result["upstream_failures"]["daemon_unreachable"] == 1


async def test_changed_endpoint_establishes_a_new_baseline():
    r = reader([cumulative(1, 5), cumulative(1, 50)])
    await r.poll()
    r.config.dashboard_server.port += 1
    assert (await r.poll())["reason"] == "epoch_reset"
