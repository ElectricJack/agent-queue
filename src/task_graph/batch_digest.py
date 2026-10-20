"""Canonical identity of a held ingestion graph loaded from live rows.

Callers hydrate each membership row with its task's title, description,
parent_task_id, acceptance, constraints and size. Pass dependency rows for the
combined graph; only edges incident to active members enter the snapshot.
This module is pure: transaction locking, revision bumps and receipt checks
belong to the batch commands/queries, which must reload rather than hash the
proposal payload or trust a previously stored digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


def normalize_description(description: str) -> str:
    """Whitespace-only edits use the same equivalence as digest computation."""
    return " ".join(description.split())


def canonical_batch_structure(
    members: Iterable[Mapping[str, Any]], edges: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Return the readable sorted snapshot, independent of database row order.

    Member keys identify internal endpoints; external endpoints retain their
    task IDs. Tagged endpoints prevent an external ID colliding with a member
    key. Historical retired members and unrelated external edges are excluded.
    Required fidelity fields must be hydrated by the caller; missing fields
    raise rather than silently attesting an incomplete snapshot.
    """
    active = [member for member in members if member["state"] == "active"]
    keys = {member["task_id"]: member["member_key"] for member in active}
    if len(keys) != len(active) or len(set(keys.values())) != len(active):
        raise ValueError("Active batch members must have unique task IDs and member keys")

    def endpoint(task_id: str | None) -> dict[str, str] | None:
        if task_id is None:
            return None
        if task_id in keys:
            return {"member_key": keys[task_id]}
        return {"task_id": task_id}

    canonical_members = [
        {
            "member_key": member["member_key"],
            "role": member["role"],
            "parent_key": endpoint(member["parent_task_id"]),
            "title": member["title"],
            "description": normalize_description(member["description"]),
            "acceptance": member["acceptance"],
            "constraints": member["constraints"],
            "section_anchor": member["section_anchor"],
            "primary_outcome": member["primary_outcome"],
            "size": member["size"],
        }
        for member in active
    ]
    canonical_edges = [
        {
            "from": endpoint(edge["task_id"]),
            "to": endpoint(edge["depends_on_task_id"]),
            "dep_type": edge["dep_type"],
            "description": edge.get("description"),
        }
        for edge in edges
        if edge["task_id"] in keys or edge["depends_on_task_id"] in keys
    ]
    return {
        "members": sorted(canonical_members, key=lambda member: member["member_key"]),
        "edges": sorted(canonical_edges, key=_canonical_json),
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def compute_structure_digest(
    members: Iterable[Mapping[str, Any]], edges: Iterable[Mapping[str, Any]]
) -> str:
    """SHA-256 of canonical UTF-8 JSON covering active members and their edges.

    Titles stay in the readable snapshot but are excluded from attestation:
    retitles and description whitespace edits preserve a review receipt under
    the spec's nonstructural allowlist (supervisor decision on L0.1, 2026-10-14).
    Priority and tags likewise never enter the structural snapshot.
    """
    snapshot = canonical_batch_structure(members, edges)
    for member in snapshot["members"]:
        del member["title"]
    return hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()
