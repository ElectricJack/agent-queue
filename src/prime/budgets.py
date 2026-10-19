"""Bounded worker previews with lossless, explicit continuation reads."""

from __future__ import annotations

import json

from src.handoffs import clip_utf8

DESCRIPTION_BYTES = 4096
CONTEXT_BYTES = 6144
TASK_SHOW_BYTES = 16 * 1024
CONTINUATION_COMMENTS_BYTES = 64 * 1024
PAGE_CHARS = 4096


def text_preview(text: str, limit: int, task_id: str, field: str) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    pointer = (
        f"\n[Preview shortened. Read the complete {field} before acting: "
        f"`aq task show {task_id} --field {field} --offset 0 --limit {PAGE_CHARS}`; "
        "follow next_offset until it is null.]"
    )
    return clip_utf8(text, max(0, limit - len(pointer.encode("utf-8")))) + pointer


def field_page(info: dict, field: str, offset: int, limit: int) -> dict:
    if field not in info:
        return {"error": f"Unknown task field: {field}"}
    if offset < 0 or not 1 <= limit <= PAGE_CHARS:
        return {"error": f"offset must be >= 0 and limit between 1 and {PAGE_CHARS}"}
    value = info[field]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    end = min(len(text), offset + limit)
    return {"id": info["id"], "field": field, "content": text[offset:end],
            "offset": offset, "next_offset": end if end < len(text) else None,
            "total_chars": len(text)}


def task_preview(info: dict) -> dict:
    """Keep the small existing shape; bound strings and collections recursively."""
    omitted: set[str] = set()

    def project(value, field, chars, items, depth=0):
        if isinstance(value, str):
            result = text_preview(value, chars, info["id"], field)
            if result != value:
                omitted.add(field)
            return result
        if isinstance(value, list):
            if len(value) > items:
                omitted.add(field)
            return [project(v, field, chars, items, depth + 1) for v in value[:items]]
        if isinstance(value, dict):
            pairs = list(value.items())
            # Retain the keys of fixed records (decisions, dependencies,
            # receipts). CLI formatters rely on those keys even in previews.
            if len(pairs) > 32 or depth > 5:
                omitted.add(field)
                pairs = pairs[:32] if depth <= 5 else []
            return {k: project(v, field, chars, items, depth + 1) for k, v in pairs}
        return value

    for chars, items in ((1500, 6), (700, 3), (300, 1)):
        omitted.clear()
        preview = {key: project(value, key, chars, items) for key, value in info.items()}
        if omitted:
            preview["preview"] = {
                "omitted_fields": sorted(omitted),
                "read": f"aq task show {info['id']} --field FIELD --offset 0 --limit {PAGE_CHARS}",
                "instruction": "Read omitted requirements before acting; follow next_offset.",
            }
        if len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) <= TASK_SHOW_BYTES:
            return preview
    # This is unreachable with the current fixed top-level task_show schema.
    return {"id": info["id"], "status": info["status"], "preview": {
        "omitted_fields": list(info),
        "read": f"aq task show {info['id']} --field FIELD --offset 0 --limit {PAGE_CHARS}",
        "instruction": "Read omitted requirements before acting; follow next_offset.",
    }}
