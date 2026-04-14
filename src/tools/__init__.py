"""Tool registry package.

Re-exports the public API so callers can use::

    from src.tools import ToolRegistry, CategoryMeta, CATEGORIES
"""

from src.tools.definitions import (
    _ALL_TOOL_DEFINITIONS,
    _CLI_CATEGORY_OVERRIDES,
    _TOOL_CATEGORIES,
)
from src.tools.registry import CATEGORIES, CategoryMeta, ToolRegistry

__all__ = [
    "CATEGORIES",
    "CategoryMeta",
    "ToolRegistry",
    "_ALL_TOOL_DEFINITIONS",
    "_CLI_CATEGORY_OVERRIDES",
    "_TOOL_CATEGORIES",
]
