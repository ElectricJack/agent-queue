"""In-memory semantic search over tool definitions.

Built once at app startup from the current tool + plugin definitions.
No disk persistence — the index is rebuilt from live data every time.
Used by the ``find_applicable_tool`` command to let agents discover
tools by describing what they want to do.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ToolIndex:
    """In-memory vector index over tool names and descriptions."""

    def __init__(self) -> None:
        self._tools: list[dict[str, str]] = []  # [{name, description}, ...]
        self._embeddings: Any = None  # numpy ndarray (N, dim)
        self._provider: Any = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    async def build(self, tools: list[dict], memory_config: Any) -> None:
        """Embed all tool definitions into memory.

        Parameters
        ----------
        tools:
            List of tool definition dicts (must have ``name`` and ``description``).
        memory_config:
            ``MemoryConfig`` dataclass — only ``embedding_provider``,
            ``embedding_model``, ``embedding_base_url``, and
            ``embedding_api_key`` are used.
        """
        try:
            import numpy as np
            from memsearch.embeddings import get_provider
        except ImportError:
            logger.info("ToolIndex: memsearch not available, semantic tool search disabled")
            return

        self._tools = [
            {"name": t["name"], "description": t.get("description", "")}
            for t in tools
            if t.get("name")
        ]
        if not self._tools:
            return

        texts = [f"{t['name']}: {t['description']}" for t in self._tools]

        try:
            provider = get_provider(
                name=memory_config.embedding_provider,
                model=memory_config.embedding_model or None,
                base_url=memory_config.embedding_base_url or None,
                api_key=memory_config.embedding_api_key or None,
            )
            raw = await provider.embed(texts)
            self._embeddings = np.array(raw, dtype=np.float32)
            self._provider = provider
            self._ready = True
            logger.info(
                "ToolIndex: indexed %d tools (%s, dim=%d)",
                len(self._tools),
                memory_config.embedding_provider,
                self._embeddings.shape[1],
            )
        except Exception:
            logger.warning("ToolIndex: failed to build embeddings", exc_info=True)

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Find tools matching a natural language description.

        Returns a list of ``{name, description, score}`` dicts sorted
        by descending cosine similarity.
        """
        if not self._ready or self._provider is None:
            return []

        import numpy as np

        try:
            raw = await self._provider.embed([query])
            query_vec = np.array(raw[0], dtype=np.float32)
        except Exception:
            logger.warning("ToolIndex: query embedding failed", exc_info=True)
            return []

        # Cosine similarity
        dots = self._embeddings @ query_vec
        norms = np.linalg.norm(self._embeddings, axis=1) * np.linalg.norm(query_vec)
        sims = dots / np.maximum(norms, 1e-10)

        top_idx = np.argsort(sims)[::-1][:top_k]
        return [
            {
                "name": self._tools[i]["name"],
                "description": self._tools[i]["description"],
                "score": round(float(sims[i]), 3),
            }
            for i in top_idx
            if sims[i] > 0.0
        ]
