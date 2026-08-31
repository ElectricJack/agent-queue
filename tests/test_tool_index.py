"""Tests for the in-memory semantic ToolIndex.

Coverage plan §plugins items 18-19.  The two tests are a pair (R6): the
first pins the degradation path (no memsearch → safe no-op), the second
is its counterfactual — with a (fake) memsearch present, build produces
a searchable index ranked by cosine similarity.
"""

from __future__ import annotations

import sys
import types

import pytest

from src.tools.tool_index import ToolIndex


class _MemoryConfig:
    embedding_provider = "stub"
    embedding_model = ""
    embedding_base_url = ""
    embedding_api_key = ""


_TOOLS = [
    {"name": "git_commit", "description": "Commit staged changes to the repository"},
    {"name": "read_file", "description": "Read a file from the workspace"},
    {"name": "delete_task", "description": "Delete a task from the queue"},
]


# Fixed embeddings: git_commit ~ query direction, read_file oblique,
# delete_task opposite (negative similarity → dropped from results).
_VECTORS = {
    "git_commit": [1.0, 0.0],
    "read_file": [1.0, 1.0],
    "delete_task": [-1.0, 0.0],
    "__query__": [1.0, 0.1],
}


class _StubProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            name = text.split(":", 1)[0]
            out.append(_VECTORS.get(name, _VECTORS["__query__"]))
        return out


@pytest.mark.asyncio
async def test_build_is_noop_without_memsearch(monkeypatch):
    """Without an importable memsearch, build() must degrade to a logged
    no-op and search() must return [] — this is what makes ToolIndex safe
    to instantiate unconditionally at daemon startup."""
    monkeypatch.setitem(sys.modules, "memsearch", None)
    monkeypatch.setitem(sys.modules, "memsearch.embeddings", None)

    idx = ToolIndex()
    await idx.build(list(_TOOLS), _MemoryConfig())

    assert idx.ready is False
    assert await idx.search("commit my changes") == []


@pytest.mark.asyncio
async def test_search_ranks_by_cosine_similarity(monkeypatch):
    """Positive control for the pair: with memsearch available, search
    returns top_k results sorted by descending cosine similarity and
    drops entries with similarity <= 0."""
    memsearch_mod = types.ModuleType("memsearch")
    embeddings_mod = types.ModuleType("memsearch.embeddings")
    embeddings_mod.get_provider = lambda **_kwargs: _StubProvider()
    memsearch_mod.embeddings = embeddings_mod
    monkeypatch.setitem(sys.modules, "memsearch", memsearch_mod)
    monkeypatch.setitem(sys.modules, "memsearch.embeddings", embeddings_mod)

    idx = ToolIndex()
    await idx.build(list(_TOOLS), _MemoryConfig())
    assert idx.ready is True

    results = await idx.search("commit my staged work", top_k=2)

    assert [r["name"] for r in results] == ["git_commit", "read_file"]
    assert results[0]["score"] > results[1]["score"] > 0
    assert results[0]["description"] == "Commit staged changes to the repository"

    # delete_task's similarity is negative — never returned, even with a
    # larger top_k.
    all_results = await idx.search("commit my staged work", top_k=5)
    assert "delete_task" not in {r["name"] for r in all_results}
