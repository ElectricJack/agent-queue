"""Poll dashboard relay counters over HTTP without importing the edge process.

Snapshots are cumulative; a reader establishes an epoch baseline and reports
only subsequent differences. Gaps discard the baseline, never implying zero
activity. HTTP runs in a thread so the daemon's sampling loop does not wait.
"""

from __future__ import annotations

import asyncio
import json
import math
import urllib.request
from collections.abc import Callable
from typing import Any

from src.metrics.histogram import is_hist, new_hist, new_sum

METRICS_PATH = "/__aq/metrics"
FAILURES = (
    "daemon_unreachable", "daemon_timeout", "daemon_bad_response", "dashboard_server_stopping",
)


def _fetch(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value) and value >= 0
    )


def _validate(snapshot: Any) -> None:
    """Reject incomplete counters rather than publishing a misleading zero."""
    if not isinstance(snapshot, dict) or not _number(snapshot.get("epoch")):
        raise ValueError("not a relay snapshot")
    for key in ("http", "ws_handshake"):
        hist = snapshot.get(key)
        if not is_hist(hist) or not all(
            _number(value) for value in [*hist["counts"], hist.get("count"),
                                        hist.get("sum"), hist.get("max")]
        ):
            raise ValueError("not a relay histogram")
        if sum(hist["counts"]) != hist["count"]:
            raise ValueError("inconsistent relay histogram")
    failures = snapshot.get("upstream_failures")
    if not isinstance(failures, dict) or not all(_number(failures.get(k)) for k in FAILURES):
        raise ValueError("not relay failure counters")
    if not _number(snapshot.get("relays_open")):
        raise ValueError("not a relay gauge")


def _delta_hist(previous: dict, current: dict) -> dict[str, Any]:
    out = new_hist()
    out["counts"] = [
        max(0, c - p) for p, c in zip(previous["counts"], current["counts"], strict=True)
    ]
    out["count"] = max(0, current["count"] - previous["count"])
    out["sum"] = max(0.0, current["sum"] - previous["sum"])
    # Only the cumulative maximum is available; percentiles use delta buckets.
    out["max"] = current["max"]
    return out


class RelayReader:
    def __init__(
        self, config, *, fetch: Callable[[str, float], dict] | None = None, timeout: float = 1.0,
    ) -> None:
        self.config = config
        self._fetch = fetch or _fetch
        self._timeout = timeout
        self._previous: dict | None = None
        self._previous_url: str | None = None

    def reset(self) -> None:
        """Forget observations across unavailable or disabled intervals."""
        self._previous = None
        self._previous_url = None

    def url(self) -> str | None:
        server = getattr(self.config, "dashboard_server", None)
        if server is None or not getattr(server, "enabled", False):
            return None
        host = str(server.host)
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{server.port}{METRICS_PATH}"

    async def poll(self) -> dict[str, Any]:
        """Never raise on a failed probe, malformed snapshot or changed epoch."""
        try:
            url = self.url()
            if url is None:
                self.reset()
                return {"available": False, "reason": "dashboard_server_disabled"}
            current = await asyncio.to_thread(self._fetch, url, self._timeout)
            _validate(current)
            previous = self._previous
            if previous is None or self._previous_url != url or previous["epoch"] != current["epoch"]:
                result = {
                    "available": True, "reason": "epoch_reset", "relays_open": current["relays_open"],
                }
            else:
                result = {
                    "available": True,
                    "reason": None,
                    "http": _delta_hist(previous["http"], current["http"]),
                    "ws_handshake": _delta_hist(previous["ws_handshake"], current["ws_handshake"]),
                    "upstream_failures": new_sum(**{
                        k: max(0, current["upstream_failures"][k] - previous["upstream_failures"][k])
                        for k in FAILURES
                    }),
                    "relays_open": current["relays_open"],
                }
            self._previous, self._previous_url = current, url
            return result
        except Exception:  # An unavailable probe is an honest gap, never a sampler failure.
            self.reset()
            return {"available": False, "reason": "dashboard_server_unreachable"}
