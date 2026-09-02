"""Public command contract boundary.

Importing this package does **not** register anything: the built-in contracts
load on the first read of :data:`CONTRACTS` (``registry.ContractRegistry``).
Registration validates every effect clause against the renderer in
``src.playbooks.explanation``, so doing it as an import-time side effect made
``import src.playbooks.explanation`` a circular import — the package
``__init__`` re-entered that module before ``can_render`` was defined.
"""

from src.commands.contracts.builtin import register_builtin_contracts
from src.commands.contracts.models import CommandContract, CommandResult
from src.commands.contracts.registry import (
    CONTRACTS,
    CommandRegistration,
    ContractRegistrationError,
    ContractRegistry,
    UnknownContract,
)

__all__ = [
    "CONTRACTS",
    "CommandContract",
    "CommandRegistration",
    "CommandResult",
    "ContractRegistrationError",
    "ContractRegistry",
    "UnknownContract",
    "register_builtin_contracts",
]
