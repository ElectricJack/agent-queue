"""Factory for creating the correct messaging adapter based on config.

Usage::

    from src.messaging import create_messaging_adapter

    adapter = create_messaging_adapter(config, orchestrator)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.messaging.base import MessagingAdapter

if TYPE_CHECKING:
    from src.config import AppConfig
    from src.orchestrator import Orchestrator


def create_messaging_adapter(
    config: "AppConfig",
    orchestrator: "Orchestrator",
) -> MessagingAdapter:
    """Instantiate the messaging adapter for the configured platform.

    Parameters
    ----------
    config:
        Application configuration — ``config.messaging_platform`` selects
        which adapter to create (``"discord"`` or ``"none"``).
    orchestrator:
        The orchestrator instance to wire into the adapter.

    Returns
    -------
    MessagingAdapter
        A concrete adapter for the selected platform.

    Raises
    ------
    ValueError
        If ``config.messaging_platform`` is not a recognised platform name.
        Telegram support was removed in the M0 messaging strip
        (docs/specs/design/messaging-rework.md §4.6) — ``"telegram"`` gets
        a dedicated, actionable error rather than falling through to the
        generic "unknown platform" message.
    """
    platform = config.messaging_platform

    if platform == "discord":
        from src.discord.adapter import DiscordMessagingAdapter

        return DiscordMessagingAdapter(config, orchestrator)
    elif platform == "none":
        from src.messaging.null_adapter import NullMessagingAdapter

        return NullMessagingAdapter(config, orchestrator)
    elif platform == "telegram":
        raise ValueError(
            "messaging_platform: 'telegram' is no longer supported — Telegram "
            "support was removed in the messaging rework (M0). Set "
            "messaging_platform to 'discord' or 'none' in config.yaml. "
            "See docs/specs/design/messaging-rework.md"
        )
    else:
        raise ValueError(
            f"Unknown messaging platform: {platform!r}. Supported platforms: 'discord', 'none'"
        )
