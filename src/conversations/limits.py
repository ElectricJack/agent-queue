"""Every numeric bound of Discord @mention conversations (mention-routing spec §4).

Numbers, not judgement: each limit the gateway, the intake command, delivery
and maintenance enforce lives here, and ``tests/test_conversation_limits.py``
pins them to the spec.  Change a value only with the spec.
"""

from __future__ import annotations

#: Longest accepted input, in Unicode code points after normalisation.
MAX_INPUT_CHARS = 4000
#: Accepted inputs per author per sliding :data:`WINDOW_SECONDS`.
AUTHOR_WINDOW_LIMIT = 10
#: Accepted inputs per channel per sliding :data:`WINDOW_SECONDS`.
CHANNEL_WINDOW_LIMIT = 60
#: The rate-limit window, and the bucket for one rate-limit notice per author.
WINDOW_SECONDS = 600
#: Longest outbound reply, marker and dashboard pointer included.
MAX_REPLY_CHARS = 1900
#: How far back one reconnect backfill pass reads.
BACKFILL_HOURS = 24
#: Most messages one reconnect backfill pass reads.
BACKFILL_MAX_MESSAGES = 1000
#: An unanswered input gets one delay notice after this long.
SUPERVISOR_DELAY_SECONDS = 900
#: Conversation text is kept this long.
TEXT_RETENTION_DAYS = 30
#: Dedup tombstones outlive the text, so expired input never replays as fresh work.
TOMBSTONE_RETENTION_DAYS = 90

__all__ = [
    "AUTHOR_WINDOW_LIMIT",
    "BACKFILL_HOURS",
    "BACKFILL_MAX_MESSAGES",
    "CHANNEL_WINDOW_LIMIT",
    "MAX_INPUT_CHARS",
    "MAX_REPLY_CHARS",
    "SUPERVISOR_DELAY_SECONDS",
    "TEXT_RETENTION_DAYS",
    "TOMBSTONE_RETENTION_DAYS",
    "WINDOW_SECONDS",
]
