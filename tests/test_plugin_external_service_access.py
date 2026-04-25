"""External plugins may access an explicit allowlist of services.

Spec §6.3 — config and vault_watcher are safe-for-external; db, git,
workspace remain INTERNAL-only.
"""

from __future__ import annotations

import pytest

from src.plugins.base import TrustLevel


@pytest.fixture
def external_context(plugin_context_factory):
    return plugin_context_factory(
        trust_level=TrustLevel.EXTERNAL,
        services={
            "config": object(),
            "vault_watcher": object(),
            "db": object(),
            "git": object(),
        },
    )


def test_external_can_get_config(external_context):
    assert external_context.get_service("config") is not None


def test_external_can_get_vault_watcher(external_context):
    assert external_context.get_service("vault_watcher") is not None


def test_external_cannot_get_db(external_context):
    with pytest.raises(PermissionError, match="vault_watcher|config"):
        external_context.get_service("db")


def test_external_cannot_get_git(external_context):
    with pytest.raises(PermissionError):
        external_context.get_service("git")


def test_internal_can_get_db(plugin_context_factory):
    ctx = plugin_context_factory(
        trust_level=TrustLevel.INTERNAL,
        services={"db": object(), "git": object()},
    )
    assert ctx.get_service("db") is not None
    assert ctx.get_service("git") is not None
