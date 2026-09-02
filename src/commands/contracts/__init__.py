"""Public command contract boundary."""
from src.commands.contracts.models import CommandContract, CommandResult
from src.commands.contracts.registry import CONTRACTS, CommandRegistration, ContractRegistrationError, ContractRegistry, UnknownContract
from src.commands.contracts.builtin import register_builtin_contracts

register_builtin_contracts(CONTRACTS)

__all__ = ["CONTRACTS", "CommandContract", "CommandRegistration", "CommandResult", "ContractRegistrationError", "ContractRegistry", "UnknownContract"]
