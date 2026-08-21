"""Guard: gate.*/session.*/task.* events reach WebSocket clients (D3/D1/D2)."""

from __future__ import annotations

from src.api.websocket import _FORWARDED_PREFIXES


def test_forwarded_prefixes_include_wave4_events() -> None:
    for prefix in ("notify.", "message.", "gate.", "session.", "task."):
        assert prefix in _FORWARDED_PREFIXES, (
            f"Prefix '{prefix}' must be forwarded to WebSocket clients — "
            "the dashboard's gates/sessions/tasks pages rely on it for "
            "live invalidation."
        )
