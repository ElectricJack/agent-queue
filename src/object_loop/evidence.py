"""Resolve object evidence from retained identities, never transient run paths."""

from __future__ import annotations

import hashlib
import json
import os

from src.jobs.artifacts import open_artifact
from src.object_loop.artifacts import ArtifactError, artifact_uri, resolve, verify


def baseline_images(data_dir, state: dict) -> dict[str, dict]:
    """Read the exact baseline receipt and extract its per-view image identities."""
    digest = state.get("incumbent_capture_sha256")
    if not digest:
        raise ArtifactError("baseline capture identity is missing")
    with os.fdopen(open_artifact(resolve(data_dir, artifact_uri(digest)), os.O_RDONLY), "rb") as f:
        raw = f.read()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ArtifactError("baseline capture receipt does not hash to its named digest")
    receipt = json.loads(raw)
    if (receipt.get("status") != "complete"
            or receipt.get("candidate_sha256") != state["initial_incumbent_sha256"]
            or receipt.get("rig_sha256") != state["rig_sha256"]):
        raise ArtifactError("baseline capture receipt does not match the object's fixed inputs")
    return {
        name: {"uri": artifact_uri(view["image"]["sha256"]),
               "sha256": view["image"]["sha256"]}
        for name, view in receipt["views"].items()
        if name in state["mandatory_views"] and view.get("image", {}).get("sha256")
    }


def artifact_digests(data_dir, state: dict) -> set[str]:
    """Identities named by this object's durable state and verified baseline receipt."""
    found = set()

    def collect(value):
        if isinstance(value, dict):
            uri, digest = value.get("uri"), value.get("sha256")
            if isinstance(digest, str) and uri == f"artifact://sha256/{digest}":
                found.add(digest)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(state)
    if state.get("incumbent_capture_sha256"):
        found.add(state["incumbent_capture_sha256"])
    try:
        collect(baseline_images(data_dir, state))
    except (ArtifactError, OSError, ValueError, KeyError, TypeError, AttributeError):
        # A missing/corrupt receipt cannot grant access to image identities.
        pass
    return found


def final_evidence(data_dir, state: dict) -> dict:
    """A comparison packet with verified image paths or explicit per-view gaps."""
    baseline_error = None
    try:
        before = baseline_images(data_dir, state)
    except (ArtifactError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        before, baseline_error = {}, str(exc)
    best = state.get("best_receipt")
    after = {
        capture["view_id"]: capture.get("image")
        for capture in (best or {}).get("capture_receipts", [])
    } if best else before

    def image(pointer, missing):
        if not pointer:
            return {"verified": False, "error": missing}
        try:
            return verify(data_dir, pointer["uri"], pointer["sha256"])
        except (ArtifactError, OSError, ValueError, KeyError, TypeError) as exc:
            return {"uri": pointer.get("uri"), "sha256": pointer.get("sha256"),
                    "verified": False, "error": str(exc)}

    return {
        "baseline_capture_uri": (artifact_uri(state["incumbent_capture_sha256"])
                                 if state.get("incumbent_capture_sha256") else None),
        "mandatory_views": state["mandatory_views"],
        "reference_kind": state.get("reference_kind", "calibrated"),
        "best_receipt": best,
        "stop_reason": state.get("stop_reason"),
        "spent": state["spent"],
        "views": {
            name: {
                "before": image(before.get(name), baseline_error or "baseline view is missing"),
                "after": image(after.get(name), "best candidate view is missing"),
            }
            for name in state["mandatory_views"]
        },
    }
