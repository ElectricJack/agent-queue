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
        """Tool definitions for one node: exactly the names in ``allowed``.

        Two behaviours changed in Playbook V2 Package 0 §3.1/§5.4:

        - ``allowed is None`` (no policy declared) now means **no tools**, not
          the registry's full catalogue.  "Missing means everything" is the
          same default-open shape as an empty capability set meaning "all",
          and the spec forbids it.  A playbook that needs tools names them.
        - An unknown name is **filtered** rather than raised on.  A policy is
          an allowlist, and a name the registry does not (yet) know is simply
          not granted; raising turned a narrowing intent into a hard failure
          at run time.
        """
        known = {t["name"]: t for t in self.tool_registry.get_all_tools()}
        if allowed is None:
            return []
        tools = [known[n] for n in allowed if n in known]
        return [t for t in tools if t["name"] not in _EXCLUDED_TOOLS]

    @classmethod
    def for_tests(cls, llm: "LLMClient") -> "PlaybookServices":
        registry = MagicMock()
        registry.get_core_tools.return_value = []
        registry.get_all_tools.return_value = []
        handler = MagicMock()
        handler.execute = AsyncMock(return_value={"success": True})
        return cls(llm=llm, handler=handler, tool_registry=registry)
