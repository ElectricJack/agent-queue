"""The single command-contract registry."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from src.commands.contracts.models import CommandArgs, CommandContract, CommandResult
from src.commands.principal import ExecutionPrincipal

CommandContext = ExecutionPrincipal
InvokeAdapter = Callable[[CommandArgs, CommandContext], Awaitable[CommandResult[Any]]]
PreviewAdapter = InvokeAdapter


class ContractRegistrationError(ValueError):
    pass


class UnknownContract(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class CommandRegistration:
    name: str
    contract: CommandContract[Any, Any]
    invoke: InvokeAdapter
    preview: PreviewAdapter | None = None


class ContractRegistry:
    def __init__(self) -> None:
        self._registrations: dict[str, CommandRegistration] = {}

    def register(self, registration: CommandRegistration) -> None:
        if registration.name in self._registrations:
            raise ContractRegistrationError(f"contract {registration.name!r} is already registered")
        if registration.name != registration.contract.name:
            raise ContractRegistrationError("registration name does not match its contract")
        preview = registration.preview is not None
        if registration.contract.execution.supports_preview != preview:
            raise ContractRegistrationError("preview adapter must exactly match supports_preview")
        # Deliberately deferred to prevent the contracts -> playbooks import cycle.
        from src.playbooks.explanation import can_render
        for clause in registration.contract.execution.effects:
            if not can_render(clause):
                raise ContractRegistrationError(f"effect clause {clause.kind!r} has no renderer")
        self._registrations[registration.name] = registration

    def get(self, name: str) -> CommandRegistration | None:
        return self._registrations.get(name)

    def require(self, name: str) -> CommandRegistration:
        registration = self.get(name)
        if registration is None:
            raise UnknownContract(name)
        return registration

    def names(self) -> frozenset[str]:
        return frozenset(self._registrations)

    def fingerprint(self, name: str) -> str:
        return self.require(name).contract.fingerprint()

    def registry_fingerprint(self) -> str:
        document = {name: self.fingerprint(name) for name in sorted(self.names())}
        return "sha256:" + hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def required_capability(self, name: str) -> str | None:
        registration = self.get(name)
        return registration.contract.execution.capability if registration else None


CONTRACTS = ContractRegistry()
