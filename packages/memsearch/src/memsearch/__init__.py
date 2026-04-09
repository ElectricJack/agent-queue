"""memsearch — semantic memory search for markdown knowledge bases."""

from .core import MemSearch
from .scoping import (
    SCOPE_WEIGHTS,
    CollectionRouter,
    MemoryScope,
    ScopeEntry,
    collection_name,
    merge_and_rank,
    parse_collection_name,
    resolve_scopes,
    sanitize_id,
    vault_paths,
)

__all__ = [
    "SCOPE_WEIGHTS",
    "CollectionRouter",
    "MemSearch",
    "MemoryScope",
    "ScopeEntry",
    "collection_name",
    "merge_and_rank",
    "parse_collection_name",
    "resolve_scopes",
    "sanitize_id",
    "vault_paths",
]
