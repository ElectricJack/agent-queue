"""Agent profile management — parsing, syncing, registry, and migration.

Submodules:
    parser           — markdown ↔ AgentProfile conversion
    sync             — vault → DB profile sync, vault watcher integration
    migration        — DB → vault markdown generation
    mcp_registry     — MCP server registry (vault-sourced, in-memory)
    mcp_probe        — MCP server tool-list probe (stdio/http)
    mcp_catalog      — in-memory tool catalog populated by probes
    mcp_inline_migration — one-shot extractor for legacy inline mcp_servers

All public symbols live on the submodules — import from there directly
rather than from the package.
"""
