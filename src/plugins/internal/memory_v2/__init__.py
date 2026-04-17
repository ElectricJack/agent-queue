"""Internal plugin package: aq-memory-v2.

Self-contained memory subsystem — service layer, background extractor,
and all command handlers live in this package.

Submodules:
- ``service``   — MemoryV2Service (async wrapper over memsearch/Milvus)
- ``extractor`` — MemoryExtractor (event-driven background extraction)
- ``plugin``    — MemoryV2Plugin (command handlers, tool definitions)
"""

from src.plugins.internal.memory_v2.plugin import (  # noqa: F401
    AGENT_TOOLS,
    TOOL_CATEGORY,
    TOOL_DEFINITIONS,
    V2_ONLY_TOOLS,
    MemoryV2Plugin,
)
