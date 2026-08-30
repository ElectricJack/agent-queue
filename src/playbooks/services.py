"""What a playbook run needs from the daemon, bundled (llm-direct-path §5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from src.commands.handler import CommandHandler
    from src.llm import LLMClient
    from src.llm_logger import LLMLogger
    from src.tools.registry import ToolRegistry

#: Navigation tools the old chat loop special-cased; a playbook node never gets them.
_EXCLUDED_TOOLS = frozenset({"load_tools", "reply_to_user"})


@dataclass
class PlaybookServices:
    llm: "LLMClient"
    handler: "CommandHandler"
    tool_registry: "ToolRegistry"
    llm_logger: "LLMLogger | None" = None
    runtimes: Any = None  # RuntimeRegistry for harness-less one-shot node sessions

    def node_tools(self, allowed: list[str] | None) -> list[dict]:
        """Tool definitions for one node: exactly ``allowed`` (validated against the
        registry) or the registry's full catalogue when the profile lists none.
        Unscoped playbooks are trusted; ``profile_id:`` (the ``allowed`` branch) is
        the sandboxing mechanism, so the unscoped default must not be limited to the
        core set."""
        if allowed is None:
            tools = self.tool_registry.get_all_tools()
        else:
            known = {t["name"]: t for t in self.tool_registry.get_all_tools()}
            unknown = sorted(set(allowed) - set(known))
            if unknown:
                raise ValueError(f"Unknown tool names in profile allowed_tools: {unknown}")
            tools = [known[n] for n in allowed]
        return [t for t in tools if t["name"] not in _EXCLUDED_TOOLS]

    @classmethod
    def for_tests(cls, llm: "LLMClient") -> "PlaybookServices":
        registry = MagicMock()
        registry.get_core_tools.return_value = []
        registry.get_all_tools.return_value = []
        handler = MagicMock()
        handler.execute = AsyncMock(return_value={"success": True})
        return cls(llm=llm, handler=handler, tool_registry=registry)
