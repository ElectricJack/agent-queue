"""The vault_watcher service is the safe-for-external Protocol facade."""

from __future__ import annotations

from src.plugins.services import VaultWatcherService


class _StubVaultWatcher:
    """Minimal duck-typed VaultWatcher for the Protocol check."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, object, str | None]] = []
        self.unregistered: list[str] = []

    def register_handler(self, pattern, handler, *, handler_id=None):
        self.registered.append((pattern, handler, handler_id))
        return handler_id or f"auto:{pattern}"

    def unregister_handler(self, handler_id):
        self.unregistered.append(handler_id)


def test_vault_watcher_service_protocol_satisfied_by_stub():
    """VaultWatcherService is a runtime_checkable Protocol covering register/unregister."""
    stub = _StubVaultWatcher()
    assert isinstance(stub, VaultWatcherService)


def test_vault_watcher_service_registers_and_unregisters():
    """The Protocol contract round-trips through the stub."""
    svc: VaultWatcherService = _StubVaultWatcher()

    async def _handler(_):
        pass

    hid = svc.register_handler("FACTS.md", _handler, handler_id="facts:FACTS.md")
    assert hid == "facts:FACTS.md"

    svc.unregister_handler(hid)
    assert hid in svc.unregistered  # type: ignore[attr-defined]
