"""One observation of one provider limit window.

The shape is deliberately provider-agnostic: Codex snapshots arrive
passively off a rollout's ``rate_limits`` block, Claude snapshots come from
the ``/usage`` probe, and nothing downstream of this dataclass cares which.
It is a plain value object with no database or I/O coupling so the parser
(:mod:`src.providers.claude_usage`) can be a pure function.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ProviderUsageSnapshot"]


@dataclass(frozen=True, slots=True)
class ProviderUsageSnapshot:
    """A ``(provider, window, scope)`` series' reading at ``observed_at``.

    ``used_percent`` is 0-100 as the provider reports it.  ``resets_at`` is
    an absolute epoch, or ``None`` when the source gave a percentage without
    a usable clock — a number without a countdown still beats nothing.
    """

    provider: str
    window: str
    used_percent: float
    observed_at: float
    source: str
    scope: str = ""
    account_label: str = ""
    resets_at: float | None = None
