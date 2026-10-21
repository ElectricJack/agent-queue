"""The single command-contract registry."""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from src.commands.contracts.models import CommandContract, CommandResult
from src.commands.principal import ExecutionPrincipal
from src.docs_urls import DEFAULT_DOCS_BASE_URL, command_docs_url

CommandContext = ExecutionPrincipal
if TYPE_CHECKING:
    from src.commands.descriptors import DescriptorSnapshot

InvokeAdapter = Callable[[BaseModel, CommandContext], Awaitable[CommandResult[Any]]]
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
    """A registry of typed command contracts.

    ``autoload`` makes the built-in contracts arrive on the first *read*
    rather than as an import-time side effect of ``src.commands.contracts``.
    That is what keeps ``import src.playbooks.explanation`` working: the
    package ``__init__`` can no longer re-enter a half-built ``explanation``
    module to reach ``can_render`` (child plan §3.5 keeps the renderer in the
    playbook layer, so the deferred import inside ``register`` is the only
    direction this dependency may run).  Tests build a bare
    ``ContractRegistry()``, which loads nothing.
    """

    def __init__(
        self, *, autoload: bool = False, docs_base_url: str = DEFAULT_DOCS_BASE_URL
    ) -> None:
        self._registrations: dict[str, CommandRegistration] = {}
        self._autoload = autoload
        self._loading = False
        self._lock = threading.RLock()
        self._docs_base_url = docs_base_url
        self._discover_descriptors = autoload
        self._snapshot: DescriptorSnapshot | None = None
        self._snapshot_provider: Callable[[], DescriptorSnapshot] | None = None
        self._generated_names: set[str] = set()

    def _ensure_loaded(self) -> None:
        """Register the built-ins once, on first read."""
        if not self._autoload and not self._discover_descriptors:
            return
        with self._lock:
            if self._loading:
                return
            self._loading = True
            try:
                # Deferred so importing this module never imports the
                # contract definitions, which import this module back.
                if self._autoload:
                    from src.commands.contracts.builtin import register_builtin_contracts

                    register_builtin_contracts(self)
                    self._autoload = False
                if self._snapshot_provider:
                    snapshot = self._snapshot_provider()
                elif self._snapshot is None:
                    from src.commands.command_inventory import offline_snapshot

                    snapshot = offline_snapshot(self.typed_contracts())
                else:
                    snapshot = self._snapshot
                if snapshot is not self._snapshot:
                    self.replace_descriptors(snapshot)
            finally:
                self._loading = False

    def typed_contracts(self) -> dict[str, CommandContract[Any, Any]]:
        """Only legacy registrations are inputs to discovery; never generated ones."""
        self._ensure_loaded()
        return {
            name: registration.contract for name, registration in self._registrations.items()
            if name not in self._generated_names
        }

    @property
    def descriptors(self) -> DescriptorSnapshot:
        self._ensure_loaded()
        if self._snapshot is None:
            raise RuntimeError("This registry has no descriptor snapshot")
        return self._snapshot

    def configure_snapshot_provider(self, provider: Callable[[], DescriptorSnapshot] | None) -> None:
        """Late composition after handler construction; None restores offline discovery."""
        with self._lock:
            self._snapshot_provider = provider
            self._snapshot = None

    def replace_descriptors(self, snapshot: DescriptorSnapshot) -> None:
        """Atomically replace generated registrations, retaining every typed adapter."""
        from src.commands.contracts.generated import generated_registration

        with self._lock:
            typed = {
                name: r for name, r in self._registrations.items() if name not in self._generated_names
            }
            generated = {
                name: self._with_docs_url(generated_registration(descriptor))
                for name, descriptor in snapshot.commands.items()
                if name not in typed and descriptor.denial_reason is None
                and (descriptor.origin in {"handler", "plugin"})
            }
            self._registrations = typed | generated
            self._generated_names = set(generated)
            self._snapshot = snapshot

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
        self._registrations[registration.name] = self._with_docs_url(registration)

    def _with_docs_url(self, registration: CommandRegistration) -> CommandRegistration:
        """Attach the convention-derived help URL without changing execution.

        Presentation metadata deliberately sits outside the execution
        fingerprint, so changing the configured documentation host never makes
        an already validated playbook artifact stale.
        """
        contract = registration.contract
        presentation = contract.presentation.model_copy(
            update={"help_url": command_docs_url(self._docs_base_url, registration.name)}
        )
        return replace(
            registration,
            contract=contract.model_copy(update={"presentation": presentation}),
        )

    def configure_docs_base_url(self, base_url: str) -> None:
        """Apply an installation's ``docs.base_url`` to all registrations."""
        if not isinstance(base_url, str) or not base_url.strip():
            base_url = DEFAULT_DOCS_BASE_URL
        with self._lock:
            self._ensure_loaded()
            self._docs_base_url = base_url
            self._registrations = {
                name: self._with_docs_url(registration)
                for name, registration in self._registrations.items()
            }

    def catalog(self) -> tuple[dict[str, Any], ...]:
        """Discovery includes unavailable and denied descriptors without registering them."""
        self._ensure_loaded()
        descriptors = self._snapshot.commands if self._snapshot else {}
        rows = []
        for name in sorted(descriptors.keys() | self._registrations.keys()):
            descriptor = descriptors.get(name)
            registration = self._registrations.get(name)
            presentation = registration.contract.presentation if registration else None
            row = {
                "name": name,
                "title": presentation.title if presentation else name.replace("_", " ").title(),
                "summary": presentation.summary if presentation else descriptor.description,
                "docs_url": command_docs_url(self._docs_base_url, name),
                "parameters_schema": (descriptor.input_schema() if descriptor else
                                      registration.contract.execution.args_model.model_json_schema()),
            }
            if descriptor:
                row.update(descriptor.execution_metadata())
                row.update(origin=descriptor.origin, plugin=descriptor.plugin,
                           available=descriptor.available,
                           execution_available=bool(descriptor.available and registration
                                                    and descriptor.result_mode == "typed"),
                           denied=descriptor.denial_reason is not None)
            rows.append(row)
        return tuple(rows)

    def get(self, name: str) -> CommandRegistration | None:
        self._ensure_loaded()
        return self._registrations.get(name)

    def require(self, name: str) -> CommandRegistration:
        registration = self.get(name)
        if registration is None:
            raise UnknownContract(name)
        return registration

    def names(self) -> frozenset[str]:
        self._ensure_loaded()
        return frozenset(self._registrations)

    def fingerprint(self, name: str) -> str:
        return self.require(name).contract.fingerprint()

    def registry_fingerprint(self) -> str:
        document = {name: self.fingerprint(name) for name in sorted(self.names())}
        return "sha256:" + hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def required_capability(self, name: str) -> str | None:
        registration = self.get(name)
        return registration.contract.execution.capability if registration else None


#: The process-wide registry.  Reading it loads the built-in contracts.
CONTRACTS = ContractRegistry(autoload=True)
