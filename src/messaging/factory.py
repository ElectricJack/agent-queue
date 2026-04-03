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
        which adapter to create (``"discord"`` or ``"telegram"``).
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
    """
    platform = config.messaging_platform

    if platform == "discord":
        from src.discord.adapter import DiscordMessagingAdapter

        return DiscordMessagingAdapter(config, orchestrator)
    elif platform == "telegram":
        from src.telegram.adapter import TelegramMessagingAdapter

        return TelegramMessagingAdapter(config, orchestrator)
    else:
        raise ValueError(
            f"Unknown messaging platform: {platform!r}. Supported platforms: 'discord', 'telegram'"
        )
