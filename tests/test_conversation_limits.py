"""The mention-routing spec's numeric bounds, pinned in one place (plan Task 3)."""

from src.conversations import limits


def test_the_numbers_are_the_spec_numbers():
    assert (limits.MAX_INPUT_CHARS, limits.AUTHOR_WINDOW_LIMIT, limits.CHANNEL_WINDOW_LIMIT,
            limits.WINDOW_SECONDS, limits.MAX_REPLY_CHARS, limits.BACKFILL_HOURS,
            limits.BACKFILL_MAX_MESSAGES, limits.SUPERVISOR_DELAY_SECONDS,
            limits.TEXT_RETENTION_DAYS, limits.TOMBSTONE_RETENTION_DAYS) == (
        4000, 10, 60, 600, 1900, 24, 1000, 900, 30, 90)
