"""Typed contract registration boundary for hierarchical integration commands."""

from __future__ import annotations

from src.commands.contracts.registry import ContractRegistry


DESIGN_INTEGRATION_COMMANDS = frozenset(
    {
        "integration_schedule_due",
        "integration_file_children",
        "integration_checkpoint_parent",
        "integration_delivery_readiness",
        "integration_parent_verify",
        "integration_complete_parent",
        "delivery_promote",
        "delivery_receipts",
        "integration_seal",
        "integration_build_candidate",
        "integration_ci_evidence",
        "integration_record_repair",
        "integration_repair_timeout",
        "integration_transfer_owner",
        "integration_mutate_hierarchy",
        "integration_reconcile_promotion",
        "integration_promote_main",
        "integration_release",
    }
)


def register_integration_contracts(registry: ContractRegistry) -> None:
    """Register contracts whose real handlers have landed.

    Task 2 deliberately registers none: later implementation tasks add a
    handler and its typed authority/redaction declaration together.  This
    keeps unavailable security-sensitive mutations out of the allowlist.
    """
    _ = registry
