"""Codex ``rate_limits`` capture off the ``token_count`` line (T2).

Codex publishes the account's own quota on the same rollout line that
reports token usage, which is the only place either harness volunteers it
without being asked.  These tests pin the reader half of that path: what
shape reaches :class:`TranscriptEntry`, and — just as important — when
nothing at all should.

Spec: ``docs/superpowers/specs/2026-09-07-provider-usage-implementation.md`` T2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.sessions.transcripts.codex import (
    CodexTranscriptReader,
    _rate_limits_from_payload,
)

CODEX_UUID = "01a02602-d8b3-7ab1-9c8a-3718b27f1348"

#: The verified block, copied from a live rollout via the implementation spec.
RATE_LIMITS = {
    "limit_id": "codex",
    "plan_type": "pro",
    "primary": {"used_percent": 88.0, "window_minutes": 10080, "resets_at": 1789135776},
    "secondary": None,
    "credits": {"has_credits": False, "balance": "0"},
}

INFO = {
    "last_token_usage": {
        "input_tokens": 13972,
        "cached_input_tokens": 6528,
        "output_tokens": 89,
    }
}


def _token_count_line(*, info=None, rate_limits=None) -> dict:
    payload: dict = {"type": "token_count", "info": info}
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return {
        "timestamp": "2026-08-21T13:28:35.000Z",
        "type": "event_msg",
        "payload": payload,
    }


def _rollout(base: Path, lines: list[dict]) -> Path:
    day_dir = base / ".codex" / "sessions" / "2026/08/21"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-08-21T13-28-35-{CODEX_UUID}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


# ---------------------------------------------------------------------------
# _rate_limits_from_payload
# ---------------------------------------------------------------------------


def test_primary_window_maps_to_a_normalized_reading():
    """The acceptance case: 88% on ``primary``, labelled with the plan."""
    assert _rate_limits_from_payload({"rate_limits": RATE_LIMITS}) == {
        "account_label": "pro",
        "windows": [
            {"window": "primary", "used_percent": 88.0, "resets_at": 1789135776.0}
        ],
    }


def test_both_windows_are_carried_when_both_are_reported():
    block = dict(RATE_LIMITS)
    block["secondary"] = {"used_percent": 12.5, "resets_at": None}
    result = _rate_limits_from_payload({"rate_limits": block})
    assert [w["window"] for w in result["windows"]] == ["primary", "secondary"]
    assert result["windows"][1] == {
        "window": "secondary",
        "used_percent": 12.5,
        "resets_at": None,
    }


def test_missing_rate_limits_is_none_not_an_empty_reading():
    """A line without the block must not manufacture a 0% quota."""
    assert _rate_limits_from_payload({"type": "token_count", "info": INFO}) is None


def test_a_block_with_no_numeric_percent_is_none():
    """Present-but-unknown is Codex declining to say, not "0% used"."""
    assert _rate_limits_from_payload({"rate_limits": {"primary": None}}) is None
    assert (
        _rate_limits_from_payload({"rate_limits": {"primary": {"used_percent": None}}})
        is None
    )
    assert (
        _rate_limits_from_payload({"rate_limits": {"primary": {"used_percent": "88"}}})
        is None
    ), "a string percent is a shape change, not a reading"


def test_account_label_falls_back_to_limit_id():
    block = {k: v for k, v in RATE_LIMITS.items() if k != "plan_type"}
    assert _rate_limits_from_payload({"rate_limits": block})["account_label"] == "codex"


def test_an_unparseable_reset_still_yields_the_percentage():
    """A number without a countdown still beats nothing."""
    block = {"plan_type": "pro", "primary": {"used_percent": 88.0, "resets_at": "soon"}}
    windows = _rate_limits_from_payload({"rate_limits": block})["windows"]
    assert windows == [{"window": "primary", "used_percent": 88.0, "resets_at": None}]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 10**400])
async def test_invalid_numeric_quota_does_not_block_following_entry(tmp_path, bad):
    block = {"primary": {"used_percent": bad}}
    path = _rollout(tmp_path, [
        _token_count_line(info=None, rate_limits=block),
        _token_count_line(info=None, rate_limits=RATE_LIMITS),
    ])
    entries, offset = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert len(entries) == 1
    assert entries[0].rate_limits["windows"][0]["used_percent"] == 88.0
    assert offset == path.stat().st_size


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 10**400])
def test_invalid_reset_keeps_percentage_without_a_clock(bad):
    block = {"primary": {"used_percent": 88.0, "resets_at": bad}}
    reading = _rate_limits_from_payload({"rate_limits": block})
    assert reading["windows"][0]["resets_at"] is None


async def test_relative_reset_uses_transcript_observation_time(tmp_path):
    block = {"primary": {"used_percent": 88.0, "resets_in_seconds": 60}}
    path = _rollout(tmp_path, [_token_count_line(info=None, rate_limits=block)])
    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    entry = entries[0]
    assert entry.rate_limits["windows"][0]["resets_at"] == entry.ts + 60


# ---------------------------------------------------------------------------
# through the reader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reader_attaches_rate_limits_to_the_billed_entry(tmp_path: Path):
    path = _rollout(tmp_path, [_token_count_line(info=INFO, rate_limits=RATE_LIMITS)])
    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.usage and entry.rate_limits
    assert entry.rate_limits["account_label"] == "pro"
    assert entry.rate_limits["windows"][0]["used_percent"] == 88.0


@pytest.mark.asyncio
async def test_a_token_count_without_rate_limits_carries_none(tmp_path: Path):
    path = _rollout(tmp_path, [_token_count_line(info=INFO)])
    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert [e.rate_limits for e in entries] == [None]


@pytest.mark.asyncio
async def test_a_rate_limits_only_line_still_produces_an_entry(tmp_path: Path):
    """Codex emits ``token_count`` with a null ``info`` and a live quota.

    Dropping it would mean the only readings we ever store are the ones that
    happen to bill tokens too.
    """
    path = _rollout(tmp_path, [_token_count_line(info=None, rate_limits=RATE_LIMITS)])
    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert len(entries) == 1
    assert entries[0].usage is None
    assert entries[0].rate_limits["windows"][0]["used_percent"] == 88.0


@pytest.mark.asyncio
async def test_a_token_count_with_neither_is_still_skipped(tmp_path: Path):
    path = _rollout(tmp_path, [_token_count_line(info=None)])
    entries, _ = await CodexTranscriptReader(base_dir=tmp_path).read_new(path, 0)
    assert entries == []


@pytest.mark.asyncio
async def test_a_quota_only_entry_does_not_read_as_an_active_turn(tmp_path: Path):
    """It has no text and bills nothing; treating it as output would keep an
    idle session looking in-turn forever."""
    path = _rollout(tmp_path, [_token_count_line(info=None, rate_limits=RATE_LIMITS)])
    reader = CodexTranscriptReader(base_dir=tmp_path)
    entries, _ = await reader.read_new(path, 0)
    assert reader.infer_activity(entries) == "idle"
