"""Compatibility exports; implementation lives in :mod:`source_repairs`."""

from src.integration.source_repairs import (
    DELIVERY_KEY as DELIVERY_KEY,
    DELIVERED as DELIVERED,
    UNDELIVERED as UNDELIVERED,
    UNKNOWN as UNKNOWN,
    retire_reopened_source_repairs_on as retire_reopened_source_repairs_on,
    source_identity as source_identity,
    SourceDeliveryProof as SourceDeliveryProof,
    prove_source_delivered as prove_source_delivered,
    queued_source_repairs as queued_source_repairs,
    delivered_queued_repairs as delivered_queued_repairs,
    delivered_repair_sources_on as delivered_repair_sources_on,
    retire_delivered_queued_repairs as retire_delivered_queued_repairs,
    record_delivery_evidence as record_delivery_evidence,
)
