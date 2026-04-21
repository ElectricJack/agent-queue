"""Tests for standalone Discord views."""

import logging
from unittest.mock import MagicMock

import discord
import pytest


def test_suggestion_view_exists():
    from src.discord.views import SuggestionView

    assert SuggestionView is not None


class TestExpiredInteractionTolerantView:
    """A view that silently consumes NotFound 10062 ("Unknown interaction").

    Discord interaction tokens expire (3s initial, 15m follow-up). When a
    user clicks a button on an expired message, discord.py's default
    on_error logs a full traceback at ERROR level, which spams the log
    without indicating a real problem. The mixin swallows only that
    specific error; everything else falls through to default behavior.
    """

    def _make_notfound(self, code: int, text: str = "Unknown") -> discord.NotFound:
        # discord.NotFound's constructor expects a response and message.
        # We fake the minimum attrs the subclass's super().__init__ uses.
        response = MagicMock()
        response.status = 404
        response.reason = "Not Found"
        err = discord.NotFound(response, {"code": code, "message": text})
        # discord.NotFound parses code into .code; double-check.
        assert err.code == code
        return err

    @pytest.mark.asyncio
    async def test_swallows_unknown_interaction(self, caplog):
        from src.discord.views import ExpiredInteractionTolerantView

        view = ExpiredInteractionTolerantView(timeout=1)
        err = self._make_notfound(10062, "Unknown interaction")
        with caplog.at_level(logging.DEBUG, logger="src.discord.views"):
            await view.on_error(MagicMock(), err, MagicMock())
        # Nothing logged at ERROR level for the expired-interaction case.
        assert not any(
            r.levelno >= logging.ERROR for r in caplog.records
        ), f"Unexpected ERROR logs: {[r.getMessage() for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_propagates_other_notfound_codes(self, caplog):
        from src.discord.views import ExpiredInteractionTolerantView

        view = ExpiredInteractionTolerantView(timeout=1)
        err = self._make_notfound(10008, "Unknown message")
        with caplog.at_level(logging.ERROR):
            # Default discord.py behavior logs to 'discord.ui.view' at ERROR.
            # We only assert our mixin does NOT silently swallow non-10062.
            await view.on_error(MagicMock(), err, MagicMock())
        # At least one ERROR-level record must be emitted for a non-10062 code.
        assert any(
            r.levelno >= logging.ERROR for r in caplog.records
        ), "Expected default ERROR logging for non-expired NotFound codes"


def test_suggestion_embed_formatter():
    from src.discord.views import format_suggestion_embed

    embed = format_suggestion_embed(
        suggestion_type="task",
        text="Create a profiling task for the particle system",
        project_id="my-game",
        confidence=0.85,
    )
    assert embed is not None
