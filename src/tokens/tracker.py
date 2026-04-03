"""Rate limit window tracking with sliding time windows.

``RateLimitWindow`` implements a simple token-bucket algorithm per time period
(per-minute, per-hour, per-day).  When token consumption within the current
window reaches the configured maximum, ``is_exceeded()`` returns True and the
orchestrator pauses task assignment for that agent type until the window resets.

This is used to detect when agents should back off to avoid API rate limits,
not to enforce hard quotas (the API itself handles that).
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RateLimitWindow:
    agent_type: str
    limit_type: str  # per_minute, per_hour, per_day
    max_tokens: int
    current_tokens: int = 0
    window_start: float = 0.0

    def __post_init__(self):
        if self.window_start == 0.0:
            self.window_start = time.time()

    @property
    def window_seconds(self) -> int:
        return {"per_minute": 60, "per_hour": 3600, "per_day": 86400}[self.limit_type]

    def is_exceeded(self) -> bool:
        if time.time() - self.window_start > self.window_seconds:
            return False  # window has reset
        return self.current_tokens >= self.max_tokens

    def seconds_until_reset(self) -> float:
        elapsed = time.time() - self.window_start
        remaining = self.window_seconds - elapsed
        return max(0.0, remaining)

    def record(self, tokens: int) -> None:
        now = time.time()
        if now - self.window_start > self.window_seconds:
            self.current_tokens = 0
            self.window_start = now
        self.current_tokens += tokens
