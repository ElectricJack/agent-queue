"""Backward-compatibility shim — ChatAgent is now Supervisor.

.. deprecated::
    Import :class:`~src.runtimes.supervisor.Supervisor` directly instead.
    This module re-exports ``Supervisor`` as ``ChatAgent`` for legacy callers.
"""

from src.runtimes.supervisor import (  # noqa: F401
    Supervisor as ChatAgent,
    TOOLS,
    SYSTEM_PROMPT_TEMPLATE,
    _tool_label,
)

__all__ = ["ChatAgent", "TOOLS", "SYSTEM_PROMPT_TEMPLATE"]
