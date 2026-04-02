# ChatAgent Specification

> **DELETED:** The `ChatAgent` class has been replaced by `Supervisor` (`src/supervisor.py`).
> `src/chat_agent.py` exists only as a backward-compatibility shim that re-exports
> `Supervisor` as `ChatAgent`, plus `TOOLS` and `SYSTEM_PROMPT_TEMPLATE`.
>
> **See [`specs/supervisor.md`](supervisor.md) for the current specification.**
>
> The original ChatAgent spec content has been removed — it described a flat 61-tool
> architecture that no longer exists (the Supervisor uses tiered tool loading via
> `ToolRegistry`). Git history preserves the original spec if needed for reference.
