"""Deterministic selection proposals; a report is never validation evidence."""

from __future__ import annotations

import shlex
from collections import Counter


def render_report(result: dict, *, mode: str, plan_only: bool) -> str:
    values = {**result.get("record", {}), **result}
    lines = [
        f"selection_id: {values.get('selection_id') or values.get('id') or 'unrecorded'}",
        f"mode: {mode}",
        f"recorded: {'yes' if values.get('recorded') else 'no'}",
        f"base: {values.get('base_ref') or '?'} ({values.get('base_sha') or '?'})",
        f"head: {values.get('head_sha') or '?'}",
        "counts: "
        + ", ".join(
            f"{label}={len(values.get(field) or [])}"
            for label, field in (
                ("M", "mandatory_modules"),
                ("S", "static_modules"),
                ("J", "jev_modules"),
                ("F", "fallback_modules"),
                ("final", "final_modules"),
            )
        ),
        f"full_required: {'yes' if values.get('full_required') else 'no'}",
        f"jev_status: {values.get('jev_status') or 'unavailable'}",
        f"fallback_reason: {values.get('fallback_reason') or 'none'}",
    ]
    reasons = Counter(
        reason for codes in (values.get("reasons") or {}).values() for reason in codes
    )
    lines.append("Top reasons:")
    lines.extend(
        f"  {reason}: {count}"
        for reason, count in sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
    )
    lines.append("Proposed commands:")
    lines.extend(shlex.join(["aq", "test", *argv]) for argv in values.get("argv", []))
    lines.append("Pending obligations:")
    lines.extend(
        f"  {item['kind']}: {item['command']}"
        for item in sorted(
            values.get("pending_obligations", []), key=lambda item: (item["kind"], item["command"])
        )
    )
    if plan_only:
        lines.append("Nothing was verified by this report.")
    return "\n".join(lines)
