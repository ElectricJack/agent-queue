"""Tool registry package.

Re-exports the public API so callers can use::

    from src.tools import ToolRegistry, CategoryMeta, CATEGORIES
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.tools.definitions import (
        _ALL_TOOL_DEFINITIONS,
        _CLI_CATEGORY_OVERRIDES,
        _FALLBACK_INPUT_SCHEMAS,
        _TOOL_CATEGORIES,
    )
    from src.tools.registry import CATEGORIES, CategoryMeta, ToolRegistry

__all__ = [
    "CATEGORIES",
    "CategoryMeta",
    "ToolRegistry",
    "_ALL_TOOL_DEFINITIONS",
    "_CLI_CATEGORY_OVERRIDES",
    "_FALLBACK_INPUT_SCHEMAS",
    "_TOOL_CATEGORIES",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = "definitions" if name.startswith("_") else "registry"
    value = getattr(import_module(f"src.tools.{module}"), name)
    globals()[name] = value
    return value
