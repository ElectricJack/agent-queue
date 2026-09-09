"""``GET /api/providers/usage`` --- each provider's own quota, as it reports it.

This is the read side of the provider-usage feature: the writers (the Codex
transcript watcher and the Claude ``/usage`` probe) append snapshots, and this
route hands the newest one per ``(provider, window, scope)`` series to the
dashboard cards and the doctor check.

The one piece of judgement here is :func:`is_stale`.  It lives server-side
because two clients deriving it independently would eventually disagree about
the same card, and because the right horizon is not a property of the client:
a Codex reading only advances while a Codex session is live, so four hours old
is normal, while a Claude probe running on a ten-minute timer is late at
twenty-five minutes.  Both horizons are config, read from one place.

Router-factory shape mirrors :mod:`src.api.metrics` so a test can wire a bare
``db`` without booting the daemon.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from src.api.models.provider import ProviderUsageResponse, ProviderUsageSnapshot

__all__ = ["build_providers_router", "is_stale", "router", "series_key"]

#: Fallback horizons, used when no config was injected.  They match
#: ``ProvidersConfig``'s defaults; the config object is authoritative when one
#: is present, and this pair only keeps a bare ``build_providers_router(db=db)``
#: honest rather than defining a third opinion.
DEFAULT_CLAUDE_STALE_AFTER = 1500.0
DEFAULT_CODEX_STALE_AFTER = 4 * 3600.0

#: Most rows one ``?since=`` response will return per series.  History here is
#: a sparkline behind a card, not a chart with a zoom control.
MAX_SERIES_POINTS = 2_000


def series_key(row: dict) -> str:
    """The ``series`` map's key for *row*: ``"<provider>/<window>/<scope>"``."""
    return f"{row['provider']}/{row['window']}/{row.get('scope') or ''}"


def _last_seen(row: dict) -> float:
    """When this reading was last *confirmed*.

    ``last_seen_at`` is the right field (spec amendment A3): an unchanged
    reading advances it in place while ``observed_at`` stays at the moment the
    value first appeared, so measuring age from ``observed_at`` would call a
    steady-but-healthy window stale.  It falls back to ``observed_at`` so a row
    written before the column existed still reports an honest age instead of
    an epoch-zero one.
    """
    value = row.get("last_seen_at")
    if value is None:
        value = row["observed_at"]
    return float(value)


def _horizon(provider: str, config) -> float:
    """Seconds after which a *provider*'s reading is stale."""
    providers = getattr(config, "providers", None) if config is not None else None
    if provider == "codex":
        if providers is not None:
            return float(providers.codex_stale_after_seconds)
        return DEFAULT_CODEX_STALE_AFTER
    if providers is not None:
        return float(providers.claude.stale_after_seconds)
    return DEFAULT_CLAUDE_STALE_AFTER


def is_stale(row: dict, now: float, config=None) -> bool:
    """True when *row* has not been confirmed inside its provider's horizon.

    A reading from the future (a clock skewed between the writer's box and
    ours) is fresh, not stale: a negative age must not wrap into a verdict.
    """
    return (now - _last_seen(row)) > _horizon(str(row.get("provider") or ""), config)


def _to_model(row: dict, now: float, config) -> ProviderUsageSnapshot:
    last_seen = _last_seen(row)
    return ProviderUsageSnapshot(
        id=int(row["id"]),
        provider=str(row["provider"]),
        account_label=str(row.get("account_label") or ""),
        window=str(row["window"]),
        scope=str(row.get("scope") or ""),
        used_percent=float(row["used_percent"]),
        resets_at=None if row.get("resets_at") is None else float(row["resets_at"]),
        observed_at=float(row["observed_at"]),
        last_seen_at=last_seen,
        source=str(row["source"]),
        stale=is_stale(row, now, config),
        age_seconds=max(0.0, now - last_seen),
    )


async def _usage(
    db,
    provider: str | None,
    since: float | None,
    config=None,
) -> ProviderUsageResponse:
    if since is not None and since < 0:
        raise HTTPException(status_code=422, detail="since must be >= 0")

    now = time.time()
    # An empty table is an empty list, not an error: "no provider has reported
    # yet" is the state every install starts in, and a 404 there would make the
    # dashboard render a failure for a feature that is merely quiet.
    rows = await db.latest_provider_usage(provider)
    snapshots = [_to_model(row, now, config) for row in rows]

    series: dict[str, list[ProviderUsageSnapshot]] = {}
    if since is not None:
        for row in rows:
            history = await db.provider_usage_series(
                str(row["provider"]),
                str(row["window"]),
                str(row.get("scope") or ""),
                since=float(since),
                limit=MAX_SERIES_POINTS,
            )
            series[series_key(row)] = [_to_model(point, now, config) for point in history]

    return ProviderUsageResponse(now=now, snapshots=snapshots, series=series)


def build_providers_router(*, db, config=None) -> APIRouter:
    """Router bound to an explicit ``db`` --- the seam tests use."""
    router = APIRouter()

    @router.get("/api/providers/usage", response_model=ProviderUsageResponse)
    async def get_provider_usage(
        provider: str | None = Query(None),
        since: float | None = Query(None),
    ) -> ProviderUsageResponse:
        return await _usage(db, provider, since, config)

    return router


def _build_default_router() -> APIRouter:
    """Registered in ``create_app`` --- resolves the shared db per request."""
    from src.api import dependencies as deps

    router = APIRouter()

    @router.get("/api/providers/usage", response_model=ProviderUsageResponse)
    async def get_provider_usage(
        provider: str | None = Query(None),
        since: float | None = Query(None),
    ) -> ProviderUsageResponse:
        orch = deps._orchestrator
        if orch is None:
            raise HTTPException(status_code=503, detail="orchestrator not ready")
        return await _usage(orch.db, provider, since, getattr(orch, "config", None))

    return router


router = _build_default_router()
