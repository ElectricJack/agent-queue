"""Provider-neutral knowledge fixture evaluation boundary.

Part of the approved plan
``projects/agent-queue/plans/2026-10-01-aq-work-and-knowledge-records-implementation-plan.md``
(sections 9 and 13): the deterministic, offline adapter boundary that K08
(``src/knowledge/context.py`` / ``budget.py``) implements selection on and
that ``scripts/evaluate-knowledge.py`` runs against.

This package exposes no live transport, no clock and no memory provider.
"""

from .evaluation_helpers import (
    KNOWN_ADAPTERS,
    KNOWN_ROLES,
    KNOWN_SOURCE_CLASSES,
    KNOWLEDGE_FIXTURE_SCHEMA_VERSION,
    Budget,
    Delivery,
    DeliveryLedger,
    FrozenClock,
    RecordRef,
    Selection,
    SelectionItem,
    TransportSpy,
    apply_budget_gate,
    apply_stale_claim,
    check_leakage,
    content_digest,
    cross_adapter_parity,
    freeze_time,
    provider_label_leakage,
    ref_for,
    select_records,
    sha256_text,
)

__all__ = [
    "KNOWN_ADAPTERS",
    "KNOWN_ROLES",
    "KNOWN_SOURCE_CLASSES",
    "KNOWLEDGE_FIXTURE_SCHEMA_VERSION",
    "Budget",
    "Delivery",
    "DeliveryLedger",
    "FrozenClock",
    "RecordRef",
    "Selection",
    "SelectionItem",
    "TransportSpy",
    "apply_budget_gate",
    "apply_stale_claim",
    "check_leakage",
    "content_digest",
    "cross_adapter_parity",
    "freeze_time",
    "provider_label_leakage",
    "ref_for",
    "select_records",
    "sha256_text",
]
