"""Inert legacy exclusion. An adapted plugin handshake belongs to K07.

This module never imports an external plugin to inspect its version. A config
string is not a handshake or permission to initialize an authoritative writer.
"""

from src.records.models import RecordError

LEGACY_MEMORY_PLUGINS = frozenset({"aq-memory", "memory"})


def check_legacy_plugin_load(name: str) -> None:
    if name in LEGACY_MEMORY_PLUGINS:
        raise RecordError(
            "knowledge.legacy_writer_conflict",
            "Legacy memory loading requires a versioned read-only adapter handshake",
        )


def check_legacy_writer(config, project_id: str, active_scopes: frozenset[str]) -> None:
    if config.legacy_memory_mode != "disabled" or active_scopes.intersection(
        {"global", "*", f"project:{project_id}"}
    ):
        raise RecordError("knowledge.legacy_writer_conflict")
