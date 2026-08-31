"""Unit tests for :mod:`src.vault_watcher`.

Covers the parts of the unified vault watcher that the rest of the suite
reaches only indirectly: the ``**`` glob matcher, handler-error isolation
during dispatch, and the debounce buffer's dedup/flush rules.
"""

from __future__ import annotations

import logging

import pytest

from src import workspace_spec_watcher
from src.vault_watcher import VaultChange, VaultWatcher


class _Clock:
    """A stand-in for the :mod:`time` module with a hand-driven clock.

    ``vault_watcher`` only ever calls ``time.time()``, so swapping the whole
    module reference lets a test drive the debounce timeline deterministically
    without sleeping or touching the global clock.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# The production patterns registered by the orchestrator, at every depth they
# are expected to hit, plus the negatives that distinguish ``**`` from ``*``.
# ``workspace_spec_watcher._match_segments`` is a near-identical private copy
# of this algorithm (analysis note B5), so the table is shared with the drift
# guard below.
DOUBLE_STAR_CASES: list[tuple[str, str, bool]] = [
    # ``**/*.md`` matches zero or more leading directory segments.
    ("note.md", "**/*.md", True),
    ("projects/note.md", "**/*.md", True),
    ("projects/app/note.md", "**/*.md", True),
    ("projects/app/memory/note.md", "**/*.md", True),
    ("projects/app/note.txt", "**/*.md", False),
    # ``**`` in the middle, with a literal segment either side.
    ("projects/app/memory/a.md", "projects/*/memory/**/*.md", True),
    ("projects/app/memory/k/deep/a.md", "projects/*/memory/**/*.md", True),
    ("projects/app/notes/a.md", "projects/*/memory/**/*.md", False),
    ("system/memory/a.md", "projects/*/memory/**/*.md", False),
    # ``**`` on both sides of a literal segment.
    ("memory/a.md", "**/memory/**/*.md", True),
    ("projects/app/memory/k/a.md", "**/memory/**/*.md", True),
    ("projects/app/notes/a.md", "**/memory/**/*.md", False),
    # Consecutive ``**`` segments collapse to a single one.
    ("x.md", "**/**/x.md", True),
    ("a/b/x.md", "**/**/x.md", True),
    ("a/b/y.md", "**/**/x.md", False),
]


@pytest.mark.parametrize(("rel_path", "pattern", "expected"), DOUBLE_STAR_CASES)
def test_double_star_pattern_matches_at_any_depth(rel_path, pattern, expected):
    """``**`` matches zero or more path segments; ``*`` stays within one."""
    assert VaultWatcher._matches_pattern(rel_path, pattern) is expected


@pytest.mark.parametrize(("rel_path", "pattern", "expected"), DOUBLE_STAR_CASES)
def test_spec_watcher_matcher_agrees_with_vault_watcher(rel_path, pattern, expected):
    """The duplicated matcher in ``workspace_spec_watcher`` must not drift.

    Two watchers deciding "does this file match" with two private copies of
    the same algorithm is a drift hazard (analysis note B5).  Running the same
    table through both pins them together until one of them is deleted.
    """
    assert workspace_spec_watcher._match_recursive(rel_path, pattern) is expected


@pytest.mark.asyncio
async def test_dispatch_isolates_a_raising_handler(tmp_path, caplog):
    """One handler raising must not stop the others from seeing the batch."""
    watcher = VaultWatcher(str(tmp_path), poll_interval=0, debounce_seconds=0)
    received: list[VaultChange] = []

    async def boom(_changes: list[VaultChange]) -> None:
        raise RuntimeError("handler exploded")

    async def collect(changes: list[VaultChange]) -> None:
        received.extend(changes)

    bad_id = watcher.register_handler("**/*.md", boom, handler_id="bad-handler")
    watcher.register_handler("**/*.md", collect, handler_id="good-handler")

    await watcher.check()  # initial snapshot
    (tmp_path / "note.md").write_text("hello")

    with caplog.at_level(logging.ERROR, logger="src.vault_watcher"):
        changes = await watcher.check()

    assert [c.rel_path for c in changes] == ["note.md"]
    assert [c.rel_path for c in received] == ["note.md"]
    assert bad_id in caplog.text
    assert "handler exploded" in caplog.text


@pytest.mark.asyncio
async def test_flush_pending_collapses_created_then_deleted(tmp_path):
    """A file created and removed inside one debounce window is a no-op."""
    watcher = VaultWatcher(str(tmp_path), poll_interval=0, debounce_seconds=5)
    received: list[VaultChange] = []
    watcher.register_handler("**/*.md", lambda changes: received.extend(changes))

    await watcher.check()  # initial snapshot
    note = tmp_path / "note.md"
    note.write_text("hello")
    await watcher.check()
    note.unlink()
    await watcher.check()

    await watcher._flush_pending(force=True)

    assert received == []
    assert watcher.get_pending_change_count() == 0


@pytest.mark.asyncio
async def test_flush_pending_created_then_modified_reports_created(tmp_path):
    """Create-then-modify collapses to a single ``created`` change."""
    watcher = VaultWatcher(str(tmp_path), poll_interval=0, debounce_seconds=5)
    received: list[VaultChange] = []
    watcher.register_handler("**/*.md", lambda changes: received.extend(changes))

    await watcher.check()  # initial snapshot
    note = tmp_path / "note.md"
    note.write_text("hello")
    await watcher.check()
    watcher._pending.append((VaultChange(str(note), "note.md", "modified"), watcher._pending[0][1]))

    await watcher._flush_pending(force=True)

    assert [(c.rel_path, c.operation) for c in received] == [("note.md", "created")]


@pytest.mark.asyncio
async def test_flush_dispatches_under_continuous_activity(tmp_path, monkeypatch):
    """Pending changes must not starve while the vault stays busy (VP-2).

    The debounce window is measured from the newest pending change, so a
    vault receiving at least one change per window used to hold the earliest
    change forever.  The max-age escape bounds that wait.
    """
    clock = _Clock()
    monkeypatch.setattr("src.vault_watcher.time", clock)

    watcher = VaultWatcher(
        str(tmp_path),
        poll_interval=0,
        debounce_seconds=2.0,
        max_pending_age_seconds=5.0,
    )
    received: list[VaultChange] = []
    watcher.register_handler("**/*.md", lambda changes: received.extend(changes))

    await watcher.check()  # initial snapshot

    # One change per second: always inside the 2s debounce window, so the
    # buffer never goes quiet.
    for i in range(10):
        (tmp_path / f"note_{i}.md").write_text("x")
        clock.advance(1.0)
        await watcher.check()
        if received:
            break

    assert received, "pending changes starved: nothing was ever dispatched"
    assert received[0].rel_path == "note_0.md"
    # The escape fired within the max-age bound, not merely eventually.
    assert clock.now - 1_000.0 <= 5.0 + watcher.debounce_seconds
