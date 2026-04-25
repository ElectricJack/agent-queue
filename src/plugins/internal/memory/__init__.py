"""Internal plugin package: aq-memory.

Self-contained memory subsystem — service layer, background extractor,
and all command handlers live in this package.

Submodules:
- ``service``   — MemoryService (async wrapper over memsearch/Milvus)
- ``extractor`` — MemoryExtractor (event-driven background extraction)
- ``plugin``    — MemoryPlugin (command handlers, tool definitions)
"""

from src.plugins.internal.memory.plugin import (  # noqa: F401
    AGENT_TOOLS,
    TOOL_CATEGORY,
    TOOL_DEFINITIONS,
    MemoryPlugin,
)
