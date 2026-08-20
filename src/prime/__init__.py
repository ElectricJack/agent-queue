"""Prime document renderer — startup context delivery for agent sessions.

See docs/specs/design/aq-surface.md §5 (design) and
docs/specs/implementation/aq-surface.md §2 (build plan). ``src/prime/`` is
chosen over ``src/context/`` deliberately (design §5.1): the module renders
*the prime document*, nothing else, and stays narrow so a returning memory
system plugs into sections 7-8 rather than the module growing into a
general context framework.
"""

from __future__ import annotations

from .models import PrimeDocument, PrimeSection
from .renderer import PrimeRenderer

__all__ = ["PrimeDocument", "PrimeSection", "PrimeRenderer"]
