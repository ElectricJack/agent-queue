"""memsearch — semantic memory search for markdown knowledge bases."""

from .core import MemSearch
from .scoping import (
    CollectionRouter,
    MemoryScope,
    collection_name,
    parse_collection_name,
    sanitize_id,
    vault_paths,
)

__all__ = [
    "CollectionRouter",
    "MemSearch",
    "MemoryScope",
    "collection_name",
    "parse_collection_name",
    "sanitize_id",
    "vault_paths",
]
