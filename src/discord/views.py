"""Standalone Discord UI views.

This module contains reusable Discord UI components that can be imported
and used across the application without circular dependencies.

M0 messaging strip (docs/specs/design/messaging-rework.md §4.6 /
implementation plan §5): the chat-analyzer suggestion view/formatter
(``SuggestionView``, ``format_suggestion_embed``) was deleted along with
the chat-observer wiring it served.  ``ExpiredInteractionTolerantView`` is
kept — it has no in-tree consumer at M0 (the notes/task-report views that
used it lived in the deleted ``src/discord/commands.py``), but it is
needed again for gate/approval views ported into
``packages/aq-discord/aq_discord/gates.py`` at M2.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)

# Discord interaction-token error code emitted when a user clicks a button
# on a message whose interaction token has expired (~3s for the initial
# response, 15m for follow-ups). Surfacing these at ERROR level spams the
# log with unactionable noise; the view's on_error swallows just this code.
_UNKNOWN_INTERACTION_CODE = 10062


class ExpiredInteractionTolerantView(discord.ui.View):
    """discord.ui.View that silently drops expired-interaction errors.

    Any other failure in a child-item callback falls through to the
    default discord.py handler, preserving its ERROR-level traceback.
    """

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        if (
            isinstance(error, discord.NotFound)
            and getattr(error, "code", None) == _UNKNOWN_INTERACTION_CODE
        ):
            logger.debug(
                "Ignoring expired interaction on %s: %s", item, error
            )
            return
        await super().on_error(interaction, error, item)
