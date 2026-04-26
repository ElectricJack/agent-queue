"""Platforms layer: pluggable AI agent backends.

The orchestrator interacts with platforms exclusively through the
:class:`Platform` ABC defined in :mod:`src.platforms.base`.  This module
exposes :class:`PlatformRegistry` (added in Task 3) for looking up
platform classes by string name.
"""

from __future__ import annotations
