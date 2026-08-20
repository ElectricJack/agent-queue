"""Tests for Phase 8 — chat-analyzer suggestion outcome metrics.

Covers three layers:

1. **DB layer.** ``ChatQueryMixin`` gains a ``create_suppressed_chat_analyzer_suggestion``
   helper that records a row with ``status="suppressed"`` and a non-null
   ``suppressed_by`` gate label. ``get_analyzer_suggestion_stats`` is
   extended to return ``accept_rate``, ``dismiss_rate``, and
   ``suppression_count_by_gate`` (in addition to the existing per-status
   counts), and to optionally restrict the window via a ``since`` epoch
   timestamp.

2. **Command layer.** A new ``get_chat_analyzer_metrics`` command wraps
   the query and exposes a ``since_hours`` parameter (default 24). It
   accepts an optional ``project_id`` (None means cross-project).

The DB tests use the real SQLite adapter (matches how Phase 1's
``test_database_modular.TestChatQueries`` exercises the same module);
the command-layer tests use a real ``CommandHandler`` over the same
adapter.

Note: the Discord-bot-layer tests that exercised
``AgentQueueBot._post_observation_suggestion`` were removed in the M0
messaging strip (messaging-rework §4.6) — the chat-observer/suggestion
wiring was paused along with its Discord views. The DB and command
layers above are unaffected and still exercised directly.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.database import SQLiteDatabaseAdapter


@pytest.fixture
async def db(tmp_path):
    """Provide an initialized SQLiteDatabaseAdapter."""
    database = SQLiteDatabaseAdapter(str(tmp_path / "metrics.db"))
    await database.initialize()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


class TestSuppressedSuggestionInsert:
    """The new helper writes a status=suppressed row tagged with the gate."""

    async def test_creates_suppressed_row_with_gate(self, db):
        row_id = await db.create_suppressed_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=123,
            suggestion_type="task",
            suggestion_text="parroted user request",
            suggestion_hash="hash-suppressed-1",
            suppressed_by="confidence",
        )
        assert row_id > 0
        s = await db.get_suggestion(row_id)
        assert s is not None
        assert s["status"] == "suppressed"
        assert s["suppressed_by"] == "confidence"
        assert s["suggestion_text"] == "parroted user request"

    async def test_suppressed_rows_do_not_pollute_dedup(self, db):
        """Phase 2's dedup gate keys on ``suggestion_hash`` regardless of
        status. We don't want a suppressed row to *accidentally* dedupe
        a later, legitimately high-signal suggestion with the same text —
        but the schema requires the hash, so we keep the existing
        semantics: same hash means same suggestion, dedup applies. This
        test pins that behaviour so a future refactor doesn't surprise us.
        """
        await db.create_suppressed_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=123,
            suggestion_type="task",
            suggestion_text="x",
            suggestion_hash="hash-A",
            suppressed_by="confidence",
        )
        # Same project + same hash → exists() True.
        assert await db.get_suggestion_hash_exists("p-1", "hash-A") is True
        # Different hash → False.
        assert await db.get_suggestion_hash_exists("p-1", "hash-B") is False


class TestExtendedSuggestionStats:
    """``get_analyzer_suggestion_stats`` returns accept/dismiss rates and
    suppression breakdown alongside the existing per-status counts."""

    async def _seed(self, db):
        # 1 accepted, 1 dismissed, 1 pending, 2 suppressed (confidence + dedup)
        accepted_id = await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="acc",
            suggestion_hash="h-acc",
        )
        await db.resolve_chat_analyzer_suggestion(accepted_id, "accepted")

        dismissed_id = await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="dis",
            suggestion_hash="h-dis",
        )
        await db.resolve_chat_analyzer_suggestion(dismissed_id, "dismissed")

        await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="pend",
            suggestion_hash="h-pend",
        )

        await db.create_suppressed_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="conf-supp",
            suggestion_hash="h-supp-1",
            suppressed_by="confidence",
        )
        await db.create_suppressed_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="dedup-supp",
            suggestion_hash="h-supp-2",
            suppressed_by="dedup",
        )

    async def test_per_status_counts_and_total(self, db):
        await self._seed(db)
        stats = await db.get_analyzer_suggestion_stats(project_id="p-1")
        assert stats["total"] == 5
        assert stats["pending"] == 1
        assert stats["accepted"] == 1
        assert stats["dismissed"] == 1
        assert stats["suppressed"] == 2
        # Existing field kept for back-compat (always present, may be 0).
        assert stats["auto_executed"] == 0

    async def test_accept_and_dismiss_rate(self, db):
        await self._seed(db)
        stats = await db.get_analyzer_suggestion_stats(project_id="p-1")
        # accept_rate = accepted / (accepted + dismissed) = 1/2 = 0.5
        assert stats["accept_rate"] == pytest.approx(0.5)
        # dismiss_rate = dismissed / (accepted + dismissed) = 1/2 = 0.5
        assert stats["dismiss_rate"] == pytest.approx(0.5)

    async def test_rates_when_no_resolved_suggestions(self, db):
        # Only a pending row → no resolution data → rates are None
        # (rather than ZeroDivisionError or a misleading 0.0).
        await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="x",
            suggestion_hash="h",
        )
        stats = await db.get_analyzer_suggestion_stats(project_id="p-1")
        assert stats["accept_rate"] is None
        assert stats["dismiss_rate"] is None

    async def test_suppression_count_by_gate(self, db):
        await self._seed(db)
        stats = await db.get_analyzer_suggestion_stats(project_id="p-1")
        assert stats["suppression_count_by_gate"] == {
            "confidence": 1,
            "dedup": 1,
        }

    async def test_suppression_breakdown_excludes_other_projects(self, db):
        await self._seed(db)
        # Add a suppression in a different project — must NOT bleed into p-1.
        await db.create_suppressed_chat_analyzer_suggestion(
            project_id="p-2",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="other",
            suggestion_hash="h-other",
            suppressed_by="confidence",
        )
        stats_p1 = await db.get_analyzer_suggestion_stats(project_id="p-1")
        assert stats_p1["suppression_count_by_gate"] == {
            "confidence": 1,
            "dedup": 1,
        }
        # Cross-project view aggregates everything.
        stats_all = await db.get_analyzer_suggestion_stats()
        assert stats_all["suppression_count_by_gate"]["confidence"] == 2
        assert stats_all["suppression_count_by_gate"]["dedup"] == 1

    async def test_since_filter_excludes_old_rows(self, db):
        # Insert a row, then advance the clock and insert another. Filter
        # by ``since`` between the two and only the newer row is counted.
        old_id = await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="old",
            suggestion_hash="h-old",
        )
        await db.resolve_chat_analyzer_suggestion(old_id, "accepted")
        # Backdate the row by hand — created_at is set to time.time() at
        # insert; we need to push it before the cutoff.
        from sqlalchemy import update as _update
        from src.database.tables import chat_analyzer_suggestions as cas

        cutoff = time.time() - 1.0
        async with db._engine.begin() as conn:
            await conn.execute(
                _update(cas).where(cas.c.id == old_id).values(created_at=cutoff - 60)
            )

        new_id = await db.create_chat_analyzer_suggestion(
            project_id="p-1",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="new",
            suggestion_hash="h-new",
        )
        await db.resolve_chat_analyzer_suggestion(new_id, "dismissed")

        stats = await db.get_analyzer_suggestion_stats(project_id="p-1", since=cutoff)
        # Only the "new" row should be in scope.
        assert stats["total"] == 1
        assert stats["dismissed"] == 1
        assert stats["accepted"] == 0


# ---------------------------------------------------------------------------
# Command layer
# ---------------------------------------------------------------------------


def _make_handler(db):
    """Build a real CommandHandler with a real DB and stub config/orch."""
    from src.commands.handler import CommandHandler

    orch = MagicMock()
    orch.db = db
    config = MagicMock()
    return CommandHandler(orch, config)


class TestGetChatAnalyzerMetricsCommand:
    async def test_returns_empty_metrics_for_unknown_project(self, db):
        handler = _make_handler(db)
        result = await handler.execute(
            "get_chat_analyzer_metrics", {"project_id": "missing"}
        )
        assert "error" not in result
        assert result["project_id"] == "missing"
        assert result["since_hours"] == 24
        assert result["total"] == 0
        assert result["accept_rate"] is None
        assert result["suppression_count_by_gate"] == {}

    async def test_returns_aggregated_metrics_for_seeded_project(self, db):
        # Seed: 1 accepted + 1 dismissed + 2 suppressed (confidence + dedup)
        accepted_id = await db.create_chat_analyzer_suggestion(
            project_id="proj",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="a",
            suggestion_hash="h-a",
        )
        await db.resolve_chat_analyzer_suggestion(accepted_id, "accepted")
        dismissed_id = await db.create_chat_analyzer_suggestion(
            project_id="proj",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="d",
            suggestion_hash="h-d",
        )
        await db.resolve_chat_analyzer_suggestion(dismissed_id, "dismissed")
        await db.create_suppressed_chat_analyzer_suggestion(
            project_id="proj",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="s1",
            suggestion_hash="h-s1",
            suppressed_by="confidence",
        )
        await db.create_suppressed_chat_analyzer_suggestion(
            project_id="proj",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="s2",
            suggestion_hash="h-s2",
            suppressed_by="dedup",
        )

        handler = _make_handler(db)
        result = await handler.execute(
            "get_chat_analyzer_metrics",
            {"project_id": "proj", "since_hours": 48},
        )

        assert "error" not in result
        assert result["project_id"] == "proj"
        assert result["since_hours"] == 48
        assert result["total"] == 4
        assert result["accepted"] == 1
        assert result["dismissed"] == 1
        assert result["suppressed"] == 2
        assert result["accept_rate"] == pytest.approx(0.5)
        assert result["dismiss_rate"] == pytest.approx(0.5)
        assert result["suppression_count_by_gate"] == {
            "confidence": 1,
            "dedup": 1,
        }

    async def test_no_project_id_aggregates_across_projects(self, db):
        await db.create_suppressed_chat_analyzer_suggestion(
            project_id="proj-a",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="x",
            suggestion_hash="h-a",
            suppressed_by="confidence",
        )
        await db.create_suppressed_chat_analyzer_suggestion(
            project_id="proj-b",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="y",
            suggestion_hash="h-b",
            suppressed_by="dedup",
        )

        handler = _make_handler(db)
        result = await handler.execute("get_chat_analyzer_metrics", {})

        assert "error" not in result
        assert result["project_id"] is None
        # Cross-project total
        assert result["total"] == 2
        assert result["suppressed"] == 2
        assert result["suppression_count_by_gate"] == {
            "confidence": 1,
            "dedup": 1,
        }

    async def test_since_hours_zero_means_no_window(self, db):
        """``since_hours=0`` is treated as "all time" so admins can get
        the lifetime view without juggling a sentinel value."""
        # Backdated row.
        from sqlalchemy import update as _update
        from src.database.tables import chat_analyzer_suggestions as cas

        old_id = await db.create_chat_analyzer_suggestion(
            project_id="proj",
            channel_id=1,
            suggestion_type="task",
            suggestion_text="old",
            suggestion_hash="h-old",
        )
        async with db._engine.begin() as conn:
            await conn.execute(
                _update(cas).where(cas.c.id == old_id).values(
                    created_at=time.time() - 7 * 24 * 3600
                )
            )

        handler = _make_handler(db)

        # Default 24h: misses the week-old row.
        result_24h = await handler.execute(
            "get_chat_analyzer_metrics", {"project_id": "proj"}
        )
        assert result_24h["total"] == 0

        # since_hours=0: includes everything.
        result_all = await handler.execute(
            "get_chat_analyzer_metrics",
            {"project_id": "proj", "since_hours": 0},
        )
        assert result_all["total"] == 1

