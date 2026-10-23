"""Access-service-owned, single-flight exact-commit check-run listings."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import time

from src.git.github_activity import attributed_github_caller


@dataclass
class _Listing:
    rows: list[dict]
    expires_at: float | None


class CommitCheckRuns:
    """Share raw listings; authentication and trust evaluation stay with clients."""

    def __init__(self, *, clock=time.monotonic, pending_seconds=45.0):
        self.clock, self.pending_seconds = clock, pending_seconds
        self._listings = {}
        self._flights = {}
        self._workflows = {}

    async def get(self, repository_id, sha, load):
        key = (repository_id, sha)
        cached = self._listings.get(key)
        if cached is not None and (cached.expires_at is None or self.clock() < cached.expires_at):
            return deepcopy(cached.rows)
        task = self._flights.get(key)
        if task is None:
            # The stack-based fallback caller would disappear when create_task
            # starts the loader. Pin it before that boundary, retaining budget context.
            with attributed_github_caller():
                task = asyncio.create_task(self._load(key, load))
            self._flights[key] = task
            task.add_done_callback(self._finished)
        return deepcopy(await asyncio.shield(task))

    async def _load(self, key, load):
        try:
            rows = await load()
            terminal = bool(rows) and all(
                row.get("status") == "completed" and row.get("conclusion")
                for row in rows
            )
            if self._flights.get(key) is asyncio.current_task():
                self._listings[key] = _Listing(
                    deepcopy(rows), None if terminal else self.clock() + self.pending_seconds,
                )
            return rows
        finally:
            if self._flights.get(key) is asyncio.current_task():
                del self._flights[key]

    @staticmethod
    def _finished(task):
        # All readers may have cancelled; still retrieve a failed request's exception.
        if not task.cancelled():
            task.exception()

    def forget(self, repository_id, sha=None):
        """Retire a missing SHA, or invalidate a repository after an explicit rerun.

        A request already running can finish for its readers but cannot put stale
        rows back into the cache after invalidation.
        """
        for mapping in (self._listings, self._flights, self._workflows):
            for key in list(mapping):
                if key[0] == repository_id and (sha is None or key[1] == sha):
                    del mapping[key]

    def observe_workflows(self, repository_id, sha, rows):
        """Fresh workflow listings expose new runs and unchanged-head reruns."""
        key = (repository_id, sha)
        identity = frozenset(json.dumps([
            row.get(name) for name in ("id", "run_attempt", "check_suite_id", "status", "conclusion")
        ], sort_keys=True) for row in rows)
        previous = self._workflows.get(key)
        if previous is not None and previous != identity:
            self.forget(repository_id, sha)
        self._workflows[key] = identity
