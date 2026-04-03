"""Internal plugins — command groups extracted from CommandHandler.

Internal plugins ship with the repository and are always loaded.  They
receive ``TrustLevel.INTERNAL`` contexts with full service access, are
exempt from reserved name checks, and are not tracked in the plugins
database table.

Discovery scans this package for modules that export an
:class:`InternalPlugin` subclass.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

from src.plugins.base import InternalPlugin

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def discover_internal_plugins() -> list[tuple[str, type[InternalPlugin]]]:
    """Scan this package for InternalPlugin subclasses.

    Returns:
        List of ``(module_name, plugin_class)`` tuples.
    """
    results: list[tuple[str, type[InternalPlugin]]] = []

    package = importlib.import_module(__name__)
    for importer, modname, ispkg in pkgutil.iter_modules(
        package.__path__, prefix=f"{__name__}.",
    ):
        try:
            module = importlib.import_module(modname)
        except Exception as e:
            logger.error("Failed to import internal plugin %s: %s", modname, e)
            continue

        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, InternalPlugin)
                and obj is not InternalPlugin
                and getattr(obj, "_internal", False)
            ):
                results.append((modname, obj))
                logger.debug("Discovered internal plugin: %s.%s", modname, attr_name)

    return results
