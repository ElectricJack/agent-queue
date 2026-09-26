"""Route-template latency for the daemon's API (spec 2026-09-24 dashboard performance §4.1).

Pure ASGI on purpose: a ``BaseHTTPMiddleware`` proxies a streaming body through
its own task, which is exactly the cost this middleware must not add to an SSE
pane.  Labels are the registered route templates, so a task id in the path can
never become a metric label; streams (``text/event-stream`` responses and every
WebSocket) record the handshake and an ``open`` gauge instead of a lifetime, so
a pane left open for an hour never lands in the normal-route histogram.

The label is resolved once, before the application runs: Starlette's router
rewrites ``root_path`` and ``path_params`` in the shared scope as it descends,
so a label computed afterwards would no longer match a mount.  Every call into
the registry is guarded; the application's own exception is recorded and
re-raised unchanged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from starlette.routing import Match, Mount

from src.metrics.perf import PerfRegistry, perf_registry

__all__ = ["RouteLatencyMiddleware", "is_event_stream", "route_label"]

logger = logging.getLogger(__name__)

#: Methods kept in a label; any other token becomes ``OTHER`` so a client
#: cannot mint labels by inventing methods.
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_UNMATCHED = "unmatched"


def is_event_stream(headers: Iterable[tuple[bytes, bytes]] | None) -> bool:
    """True when the response headers declare ``text/event-stream``."""
    for name, value in headers or ():
        if name.lower() == b"content-type":
            return value.lower().startswith(b"text/event-stream")
    return False


def route_label(scope: Any) -> str:
    """``"<METHOD> <route template>"`` for ``scope``, resolved like Starlette's router.

    A full match wins; otherwise the first partial match (a wrong method, which
    the router answers with 405) names the template it hit.  A mount is labelled
    by its prefix (``GET MOUNT/static``), never by the path below it.
    """
    if scope.get("type") == "websocket":
        method = "WS"
    else:
        method = str(scope.get("method") or "GET").upper()
        if method not in _METHODS:
            method = "OTHER"
    routes = getattr(getattr(scope.get("app"), "router", None), "routes", None) or ()
    partial = None
    for route in _registered_routes(routes):
        try:
            match, _ = route.matches(scope)
        except Exception:  # noqa: BLE001, S112 - a foreign route class must not break labelling
            continue
        if match is Match.FULL:
            return _template(method, route)
        if match is Match.PARTIAL and partial is None:
            partial = route
    if partial is not None:
        return _template(method, partial)
    return f"{method} {_UNMATCHED}"


def _registered_routes(routes: Iterable[Any]) -> Iterable[Any]:
    """Expand FastAPI includes into their registered, prefixed endpoint contexts.

    Recent FastAPI versions retain includes as pathless router wrappers. Their
    effective contexts use the same templates and matchers as dispatch, including
    nested prefixes, and are cached/versioned by FastAPI. Older flattened includes
    and plain Starlette routes already have the endpoint matcher we need.
    """
    for route in routes:
        try:
            contexts = getattr(route, "effective_route_contexts", None)
            if callable(contexts):
                for context in contexts():
                    yield getattr(context, "starlette_route", None) or context
            else:
                yield route
        except Exception:  # noqa: BLE001, S112 - foreign routes cannot break instrumentation
            continue


def _template(method: str, route: Any) -> str:
    path = getattr(route, "path", None)
    if not isinstance(path, str) or (not path and not isinstance(route, Mount)):
        return f"{method} {_UNMATCHED}"
    path = path or "/"
    return f"{method} MOUNT{path}" if isinstance(route, Mount) else f"{method} {path}"


class RouteLatencyMiddleware:
    """Record route latency, stream handshakes and failures in a :class:`PerfRegistry`.

    ``registry`` defaults to the process registry, looked up per request so a
    test's :func:`~src.metrics.perf.install_registry` takes effect.
    """

    def __init__(self, app, *, registry: PerfRegistry | None = None) -> None:
        self.app = app
        self._registry = registry

    async def __call__(self, scope, receive, send) -> None:
        probe = _Probe.begin(scope, self._registry)
        if probe is None:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message) -> None:
            probe.on_send(message)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            probe.on_exception()
            raise
        finally:
            probe.on_end()


class _Probe:
    """One request's observations; every method is safe to call from the request path."""

    __slots__ = ("accepted", "label", "recorded", "reg", "started", "status", "stream")

    def __init__(self, reg: PerfRegistry, label: str, started: float, *, stream: bool) -> None:
        self.reg = reg
        self.label = label
        self.started = started
        #: Every WebSocket is a stream; an HTTP response becomes one when its
        #: headers declare ``text/event-stream``.
        self.stream = stream
        self.status: Any = None
        self.accepted = False
        #: The request's one latency observation (route or handshake) is made.
        self.recorded = False

    @classmethod
    def begin(cls, scope: Any, registry: PerfRegistry | None) -> _Probe | None:
        started = time.perf_counter()
        try:
            kind = scope.get("type")
            if kind not in ("http", "websocket"):
                return None
            reg = registry if registry is not None else perf_registry()
            if not reg.enabled:
                return None
            probe = cls(reg, route_label(scope), started, stream=kind == "websocket")
        except Exception:  # instrumentation must never fail a request
            logger.debug("route latency: request not instrumented", exc_info=True)
            return None
        if probe.label.endswith(f" {_UNMATCHED}"):
            probe._call("observe_unmatched")
        return probe

    def on_send(self, message: Any) -> None:
        try:
            kind = message.get("type")
            if kind == "http.response.start":
                self.status = message.get("status")
                if is_event_stream(message.get("headers")):
                    self.stream = True
                    self._handshake(accepted=True)
            elif kind == "websocket.accept":
                self._handshake(accepted=True)
            elif kind in ("websocket.close", "websocket.http.response.start"):
                self._handshake(accepted=False)
        except Exception:  # instrumentation must never fail a request
            logger.debug("route latency: send not observed", exc_info=True)

    def on_exception(self) -> None:
        self._call("observe_exception", self.label)
        if not self.stream:
            self._route(500)

    def on_end(self) -> None:
        if self.accepted:
            self._call("stream_closed", self.label)
        elif self.stream:
            # A WebSocket that ended without ``websocket.accept`` was refused,
            # whether it closed, raised or simply returned.
            self._handshake(accepted=False)
        else:
            self._route(self.status)

    def _handshake(self, *, accepted: bool) -> None:
        if self.recorded:
            return
        self.recorded = True
        self._call("observe_stream_handshake", self.label, self._elapsed_ms(), accepted=accepted)
        if accepted:
            self.accepted = True
            self._call("stream_opened", self.label)

    def _route(self, status: Any) -> None:
        if self.recorded:
            return
        self.recorded = True
        self._call("observe_route", self.label, self._elapsed_ms(), status)

    def _elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000.0

    def _call(self, name: str, *args: Any, **kwargs: Any) -> None:
        try:
            getattr(self.reg, name)(*args, **kwargs)
        except Exception:  # instrumentation must never fail a request
            logger.debug("route latency: %s dropped", name, exc_info=True)
