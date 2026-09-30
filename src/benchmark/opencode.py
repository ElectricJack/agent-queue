"""Extract provider/model evidence from an explicit OpenCode session export."""

from __future__ import annotations

import hashlib
import json


def observations_from_export(raw: bytes, *, task_id: str, attempt_id: str) -> list[dict]:
    """Read assistant identities without treating an export as a second token ledger."""
    data = json.loads(raw)
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        raise ValueError("OpenCode export needs a messages list")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    observed = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        info = item.get("info") or item.get("message") or item
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        metadata = info.get("metadata") or {}
        assistant = metadata.get("assistant") or info
        model = assistant.get("modelID") or info.get("modelID")
        provider = assistant.get("providerID") or info.get("providerID")
        message_id = info.get("id")
        if not model or not provider or not message_id:
            continue
        observed.append(
            {
                "task_id": task_id,
                "session_attempt_id": attempt_id,
                "message_id": str(message_id),
                "provider_id": str(provider),
                "model_id": str(model),
                "source": "opencode_export",
                "source_sha256": digest,
            }
        )
    if not observed:
        raise ValueError("OpenCode export has no assistant messages with providerID/modelID")
    return observed


__all__ = ["observations_from_export"]
