"""Non-secret caller attribution and the shared background GitHub quota reserve."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import math
import re
import sys

from src.git.github_contracts import GitHubAccessError

_component = ContextVar("github_component", default=None)
_background = ContextVar("github_background", default=False)
HOUR_SECONDS = 3600
MAX_COMPONENTS = 128


@contextmanager
def github_component(name, *, background=False):
    """Attribute nested credential, API and CLI work to its owning component."""
    component, budget = _component.set(name), _background.set(background)
    try:
        yield
    finally:
        _background.reset(budget)
        _component.reset(component)


def caller_component():
    if (name := _component.get()) is not None:
        return name
    frame = sys._getframe(1)
    try:
        while frame is not None:
            name = frame.f_globals.get("__name__", "")
            # The daemon entrypoint hosts the event loop; it does not own calls
            # made by otherwise unattributed asyncio child tasks.
            if (name.startswith("src.") and not name.startswith("src.git.")
                    and name != "src.main"):
                return name
            frame = frame.f_back
    finally:
        del frame
    return "unattributed"


@contextmanager
def attributed_github_caller():
    """Carry fallback stack attribution into a task without changing quota policy."""
    token = _component.set(caller_component())
    try:
        yield
    finally:
        _component.reset(token)


def endpoint_template(key):
    """Keep endpoint structure without repository, ref, SHA or query values."""
    key = re.sub(r"repos/[^/]+/[^/]+", "repos/{owner}/{repo}", key)
    key = re.sub(r"repositories/\d+", "repositories/{id}", key)
    key = re.sub(r"(?<=/)(?:\d+|[0-9a-f]{40})(?=/|$)", "{id}", key)
    return re.sub(r"(/(?:refs|ref|branches)/).*", r"\1{ref}", key)


class GitHubActivity:
    def __init__(self, clock):
        self.clock = clock
        self.calls = {}
        self.not_modified = {}
        self.quota = None

    def record(self, api):
        self.prune()
        name = caller_component()
        if name not in self.calls and len(self.calls) >= MAX_COMPONENTS - 1:
            name = "other components"
        buckets = self.calls.setdefault(name, deque(maxlen=HOUR_SECONDS + 1))
        second = int(self.clock())
        api_count, cli_count = int(api), int(not api)
        if buckets and buckets[-1][0] == second:
            _, old_api, old_cli = buckets.pop()
            api_count, cli_count = api_count + old_api, cli_count + old_cli
        buckets.append((second, api_count, cli_count))

    def prune(self):
        cutoff = self.clock() - HOUR_SECONDS
        for counters in (self.calls, self.not_modified):
            for name, buckets in list(counters.items()):
                while buckets and buckets[0][0] <= cutoff:
                    buckets.popleft()
                if not buckets:
                    del counters[name]

    def observe(self, headers, *, status=None):
        if status == 304:
            self.prune()
            name = caller_component()
            if name not in self.not_modified and len(self.not_modified) >= MAX_COMPONENTS - 1:
                name = "other components"
            buckets = self.not_modified.setdefault(name, deque(maxlen=HOUR_SECONDS + 1))
            second, count = int(self.clock()), 1
            if buckets and buckets[-1][0] == second:
                _, old_count = buckets.pop()
                count += old_count
            buckets.append((second, count))
        if headers.get("x-ratelimit-resource", "core") != "core":
            return
        try:
            limit = int(headers["x-ratelimit-limit"])
            remaining = int(headers["x-ratelimit-remaining"])
            reset = int(headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            return
        if limit > 0 and 0 <= remaining <= limit and reset > self.clock():
            # Concurrent responses can arrive out of order. Within a window
            # never let an older response give background work quota back.
            if self.quota and self.quota["reset"] == reset:
                remaining = min(remaining, self.quota["remaining"])
            self.quota = {"limit": limit, "remaining": remaining, "reset": reset,
                          "reserve": math.ceil(limit * 0.1)}

    def check_background(self):
        quota = self.quota
        if (_background.get() and quota and self.clock() < quota["reset"]
                and quota["remaining"] <= quota["reserve"]):
            raise GitHubAccessError(
                "rate_limited", "GitHub background work deferred to preserve quota reserve",
                retry_at=quota["reset"],
            )

    def snapshot(self):
        self.prune()
        return {
            "component_window_seconds": HOUR_SECONDS,
            "api_calls_per_hour": {name: sum(row[1] for row in buckets)
                                   for name, buckets in sorted(self.calls.items())},
            "gh_commands_per_hour": {name: sum(row[2] for row in buckets)
                                     for name, buckets in sorted(self.calls.items())},
            "not_modified_per_hour": {name: sum(row[1] for row in buckets)
                                      for name, buckets in sorted(self.not_modified.items())},
            "primary_quota": self.quota if self.quota and self.clock() < self.quota["reset"]
                             else None,
        }


def check_background_budget(git):
    """Shared preflight, also usable by pollers whose commands return errors as data."""
    access = getattr(git, "github_access", None)
    if access is not None:
        access.runner.check_backoff()
