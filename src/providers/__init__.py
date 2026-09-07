"""Each provider's own account of the quota our agents draw on.

The dashboard charts the tokens *we* spend; this package carries what the
provider says is left.  The two harnesses expose it in completely different
ways and the code follows that split rather than papering over it:

* Codex publishes ``rate_limits`` on the ``token_count`` lines the
  transcript watcher already reads — passive, no probe;
* Claude has no local equivalent, so ``claude -p "/usage"`` is run on a
  timer and its text read by :func:`~src.providers.claude_usage.parse_usage_text`.

Both land as :class:`~src.providers.snapshot.ProviderUsageSnapshot` rows and
nothing downstream cares which mechanism produced one.  Design:
``docs/superpowers/specs/2026-09-07-provider-usage-design.md``.
"""

from src.providers.claude_usage import UsageParse, parse_usage_text
from src.providers.snapshot import ProviderUsageSnapshot

__all__ = [
    "ProviderUsageSnapshot",
    "UsageParse",
    "parse_usage_text",
]
