"""aq-inbox — Gmail polling plugin with deterministic allowlist routing.

``discover_internal_plugins`` inspects direct children of
``src.plugins.internal``.  Re-exporting the implementation class here keeps
the inbox package discoverable without recursing into implementation modules.
"""

from src.plugins.internal.inbox.plugin import InboxPlugin

__all__ = ["InboxPlugin"]
