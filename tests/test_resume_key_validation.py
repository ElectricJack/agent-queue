"""A resume key must name a session the harness can actually resume.

`claude --resume <unknown-id>` exits 1 immediately. The launch is reported as
"process died while waiting for the ready prompt", the task pauses 60s, and
the failed attempt records a *new* session id that also never got a
transcript — so the next attempt has a fresh dead key to resume. The key lives
in task metadata, so the loop survives daemon restarts.

Observed live: fleet-cascade, whose session row 932c8ab1-… had no transcript
on disk.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.orchestrator.execution import ExecutionMixin


class _Reader:
    """Mirrors ClaudeTranscriptReader: exact hit, else newest, else None."""

    def __init__(self, existing: set[str], newest: str | None = None):
        self.existing = existing
        self.newest = newest

    def resolve_path(self, work_dir, session_key):
        if session_key in self.existing:
            return Path(f"/x/{session_key}.jsonl")
        if self.newest:
            return Path(f"/x/{self.newest}.jsonl")
        return None


@pytest.fixture
def mixin(monkeypatch):
    m = ExecutionMixin.__new__(ExecutionMixin)
    m.db = MagicMock()
    m.db.set_task_meta = AsyncMock()
    return m


def _patch_reader(monkeypatch, reader):
    import src.sessions.transcripts as tr

    monkeypatch.setattr(tr, "resolve_reader", lambda name, base_dir=None: reader)


class TestResumeKeyValidation:
    @pytest.mark.asyncio
    async def test_key_with_a_transcript_is_kept(self, mixin, monkeypatch):
        _patch_reader(monkeypatch, _Reader(existing={"good-key"}))
        got = await mixin._validated_resume_key("claude", "/ws", "t-1", "good-key")
        assert got == "good-key"

    @pytest.mark.asyncio
    async def test_key_without_a_transcript_is_dropped(self, mixin, monkeypatch):
        """The deadlock case: resolve_path falls back to a *different* file."""
        _patch_reader(monkeypatch, _Reader(existing=set(), newest="some-other"))
        got = await mixin._validated_resume_key("claude", "/ws", "t-1", "dead-key")
        assert got is None

    @pytest.mark.asyncio
    async def test_stale_key_is_cleared_from_task_meta(self, mixin, monkeypatch):
        """Otherwise the same dead key is re-read on the next attempt."""
        _patch_reader(monkeypatch, _Reader(existing=set()))
        await mixin._validated_resume_key("claude", "/ws", "t-1", "dead-key")
        mixin.db.set_task_meta.assert_awaited_once_with("t-1", "session_resume_key", "")

    @pytest.mark.asyncio
    async def test_harness_without_a_reader_is_left_alone(self, mixin, monkeypatch):
        """codex/gemini have no reader — refusing to resume would break it."""
        _patch_reader(monkeypatch, None)
        got = await mixin._validated_resume_key("codex", "/ws", "t-1", "some-key")
        assert got == "some-key"

    @pytest.mark.asyncio
    async def test_reader_failure_does_not_block_the_launch(self, mixin, monkeypatch):
        class _Boom:
            def resolve_path(self, *a, **k):
                raise OSError("disk gone")

        _patch_reader(monkeypatch, _Boom())
        got = await mixin._validated_resume_key("claude", "/ws", "t-1", "some-key")
        assert got == "some-key"
