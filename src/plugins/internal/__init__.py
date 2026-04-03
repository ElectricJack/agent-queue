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


def collect_internal_tool_definitions() -> list[tuple[str, list[dict]]]:
    """Collect tool definitions from all internal plugin modules.

    Scans this package for modules that have a ``TOOL_CATEGORY`` constant
    and either a ``TOOL_DEFINITIONS`` list or a ``_build_tool_definitions()``
    function.  Returns the definitions grouped by category without
    instantiating any plugin classes.

    Returns:
        List of ``(category, tool_definitions)`` tuples.
    """
    collected: list[tuple[str, list[dict]]] = []

    package = importlib.import_module(__name__)
    for _importer, modname, _ispkg in pkgutil.iter_modules(
        package.__path__, prefix=f"{__name__}.",
    ):
        try:
            module = importlib.import_module(modname)
        except Exception as e:
            logger.error("Failed to import internal plugin %s: %s", modname, e)
            continue

        category = getattr(module, "TOOL_CATEGORY", None)
        if not category:
            continue

        # Try TOOL_DEFINITIONS constant first, then _build_tool_definitions()
        tool_defs = getattr(module, "TOOL_DEFINITIONS", None)
        if tool_defs is None:
            builder = getattr(module, "_build_tool_definitions", None)
            if callable(builder):
                try:
                    tool_defs = builder()
                except Exception as e:
                    logger.error("Failed to build tool defs from %s: %s", modname, e)
                    continue

        if tool_defs:
            collected.append((category, list(tool_defs)))
            logger.debug(
                "Collected %d tool definitions from %s (category=%s)",
                len(tool_defs), modname, category,
            )

    return collected


def collect_internal_formatters() -> dict:
    """Collect CLI formatter specs from all internal plugin modules.

    Each module may export a ``CLI_FORMATTERS`` attribute — either a dict
    or a callable that returns a dict.  Returns a merged dict mapping
    command names to ``FormatterSpec`` instances.
    """
    merged: dict = {}

    package = importlib.import_module(__name__)
    for _importer, modname, _ispkg in pkgutil.iter_modules(
        package.__path__, prefix=f"{__name__}.",
    ):
        try:
            module = importlib.import_module(modname)
        except Exception:
            continue

        cli_fmts = getattr(module, "CLI_FORMATTERS", None)
        if cli_fmts is None:
            continue
        # Support both a dict and a callable that returns a dict
        if callable(cli_fmts):
            try:
                cli_fmts = cli_fmts()
            except Exception as e:
                logger.error("Failed to build CLI formatters from %s: %s", modname, e)
                continue
        if isinstance(cli_fmts, dict):
            merged.update(cli_fmts)

    return merged
