"""Bounded, insert-once operator lifecycle audit; never publication/replay state.

One reserved system_config key per project/repository survives task archival.
Ordinary config commands edit validated YAML, not this table. The table's other
writers use disjoint fixed metrics/provider namespaces. No update/delete port
is provided here: only the cutover's stopped-writer transaction inserts an audit.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict

from sqlalchemy import insert, select

from src.database.tables import system_config
from src.integration.delivery_truth import (
    CONTAINMENT_CONTRACT_VERSION, DELIVERED_BY_OTHER_MEANS, DeliveryDisposition,
)
from src.integration.provenance import CompletionIdentity

SCHEMA = "aq.containment-cutover.v1"
AUDIT_SCHEMA = "aq.containment-audit.v1"
NAMESPACE = "integration.containment-cutover.v1:"
MAX_AUDIT_BYTES = 16 * 1024 * 1024
MAX_GENERATIONS = 10_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(manifest):
    # Observation time/token change during the required fresh apply observation.
    value = {k: v for k, v in manifest.items()
             if k not in {"digest", "observed_at", "observation_token"}}
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def audit_key(project_id, repository_id):
    CompletionIdentity(project_id, repository_id, "audit", "v1")  # bounded exact IDs
    return NAMESPACE + hashlib.sha256(canonical([project_id, repository_id]).encode()).hexdigest()


def plan_dispositions(plan):
    """Derive the only allowed audit entries from the exact reviewed manifest."""
    result, seen = [], set()
    facts = plan["facts"]
    for row in plan["rows"]:
        if row["action"] != DELIVERED_BY_OTHER_MEANS:
            continue
        identity = CompletionIdentity(facts["project"]["id"], facts["repository"]["id"],
                                      row["task_id"], row["completion_id"])
        if identity in seen:
            raise ValueError("duplicate cutover generation")
        seen.add(identity)
        choice = plan["choices"][row["completion_id"]]
        if (row["classification"] != "equivalent_only" or row["strict_state"] != "pending"
            or row["legacy_state"] != "contained" or choice["action"] != row["action"]
            or choice["source_oid"] != row["source_oid"]
            or choice["equivalence_proof"] != row["legacy_reason"]
            or not isinstance(choice["evidence"], str) or not choice["evidence"].strip()):
            raise ValueError("invalid cutover disposition evidence")
        value = DeliveryDisposition(
            identity.project_id, identity.repository_id, identity.task_id, identity.generation,
            row["target_ref"], row["source_oid"], choice["carrying_oid"],
            choice["equivalence_proof"], plan["digest"],
        )
        # Reuse all exact-OID/target/generation/digest validation from the shared reader.
        if not value.matches(value) or not value.target_ref.startswith("refs/heads/"):
            raise ValueError("invalid cutover disposition identity")
        result.append(asdict(value))
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate audit field")
        result[key] = value
    return result


def validate_audit(key, raw):
    """Reject corruption or namespace confusion rather than granting an exclusion."""
    try:
        if not isinstance(raw, str) or len(raw.encode()) > MAX_AUDIT_BYTES:
            raise ValueError("audit payload limit")
        audit = json.loads(raw, object_pairs_hook=_pairs)
        if set(audit) != {"schema", "project_id", "repository_id", "plan_digest",
                          "configuration_generation", "operator_id", "applied_at",
                          "manifest", "dispositions"}:
            raise ValueError("audit fields")
        plan = audit["manifest"]
        project, repository = plan["facts"]["project"], plan["facts"]["repository"]
        if (key != audit_key(audit["project_id"], audit["repository_id"])
            or audit["schema"] != AUDIT_SCHEMA or plan["schema"] != SCHEMA
            or audit["project_id"] != project["id"]
            or audit["repository_id"] != repository["id"]
            or repository["project_id"] != project["id"]
            or project["integration_repository_id"] != repository["id"]
            or project["status"] != "PAUSED"
            or project["hierarchical_integration_mode"] != "disabled"
            or project["hierarchical_integration_desired_mode"] != "disabled"
            or project["hierarchical_integration_draining"]
            or plan["contract_version"] != CONTAINMENT_CONTRACT_VERSION
            or plan["authority"] != "unchanged" or plan["stage"] != "history_preparation"
            or plan["blockers"] or plan["writers"] or len(plan["rows"]) > MAX_GENERATIONS
            or type(audit["configuration_generation"]) is not int
            or type(project["hierarchical_integration_generation"]) is not int
            or audit["configuration_generation"] != project["hierarchical_integration_generation"] + 1
            or audit["configuration_generation"] < 1
            or type(audit["applied_at"]) not in (int, float)
            or not math.isfinite(audit["applied_at"]) or audit["applied_at"] <= 0
            or not isinstance(audit["operator_id"], str)
            or not audit["operator_id"].strip() or len(audit["operator_id"]) > 500
            or any(ord(c) < 32 for c in audit["operator_id"])
            or audit["plan_digest"] != plan["digest"] or digest(plan) != plan["digest"]
            or audit["dispositions"] != plan_dispositions(plan)):
            raise ValueError("audit binding")
        return audit
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid_containment_audit: bounded exact operator manifest required") from exc


async def load_audits(conn, bindings):
    """Read only exact reserved keys; arbitrary config values never enter this reader."""
    keys = {audit_key(project_id, repository_id) for project_id, repository_id in bindings}
    if not keys:
        return []
    rows = (await conn.execute(select(system_config).where(system_config.c.key.in_(keys)))).mappings()
    return [validate_audit(row["key"], row["value"]) for row in rows]


async def insert_audit(conn, audit):
    """Cutover owns the transaction/fences; primary-key conflicts never overwrite."""
    key, raw = audit_key(audit["project_id"], audit["repository_id"]), canonical(audit)
    validate_audit(key, raw)
    await conn.execute(insert(system_config).values(key=key, value=raw))


def build_audit(plan, operator_id, applied_at):
    facts = plan["facts"]
    audit = {
        "schema": AUDIT_SCHEMA, "project_id": facts["project"]["id"],
        "repository_id": facts["repository"]["id"], "plan_digest": plan["digest"],
        "configuration_generation": facts["project"]["hierarchical_integration_generation"] + 1,
        "operator_id": operator_id, "applied_at": applied_at, "manifest": plan,
        "dispositions": plan_dispositions(plan),
    }
    validate_audit(audit_key(audit["project_id"], audit["repository_id"]), canonical(audit))
    return audit
