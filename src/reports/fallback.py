"""Bounded deterministic report content drawn only from persisted evidence."""

from __future__ import annotations

import json

MAX_REPORT_BYTES = 32 * 1024


def build_fallback(brief: dict) -> dict:
    facts = {fact["key"]: fact for fact in brief["facts"]}

    def item(ref: str) -> dict:
        fact = facts[ref]
        detail = fact["detail"]
        return {
            "refs": [ref],
            "text": str(
                detail.get("changes")
                or detail.get("summary")
                or detail.get("subject")
                or detail.get("reason")
                or detail.get("end_reason")
                or detail.get("state")
                or fact["source"]
            )[:900],
            "task_id": fact.get("task_id"),
            "late": fact.get("late", False),
            "shipment": detail.get("shipment"),
            "prior_verification": detail.get("verification", ""),
            "verification_label": "agent-reported",
        }

    report = {
        "version": 1,
        "summary": (
            "No changes found in the report window."
            if not facts
            else f"Morning report: {len(facts)} evidence records."
        ),
        "projects": [
            {
                "id": project["id"],
                "name": project.get("name", project["id"]),
                **{
                    group: [item(ref) for ref in project[group]]
                    for group in ("landed", "pending", "failures")
                },
                # The collector has no versioned surface map yet. Unknown surfaces
                # produce no invented user workflow or manual verification claim.
                "manual_checks": [],
            }
            for project in brief["projects"]
        ],
        "coverage": {**brief["coverage"], "window": brief["window"]},
        "global_facts": [item(ref) for ref, fact in facts.items() if fact["project_id"] is None],
        "omitted": {"items": 0},
    }
    while len(json.dumps(report, ensure_ascii=False).encode()) > MAX_REPORT_BYTES:
        groups = [
            project[group]
            for project in report["projects"]
            for group in ("pending", "landed", "failures")
            if project[group]
        ]
        if report["global_facts"]:
            report["global_facts"].pop()
        elif groups:
            max(groups, key=len).pop()
        else:
            raise ValueError("report metadata exceeds 32 KiB")
        report["omitted"]["items"] += 1
    return report
