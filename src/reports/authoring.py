"""Validate morning prose against frozen evidence; the daemon owns provenance."""

from __future__ import annotations

import copy
import json
import re

from src.digest.render import sanitise
from src.reports.fallback import MAX_REPORT_BYTES, build_fallback

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
_SURFACES = {
    "agent-queue": {
        "dashboard": ("dashboard/src/",),
        "CLI": ("src/cli/",),
        "reports": ("src/reports/", "src/commands/report_commands.py"),
    }
}


def surface_map(git_reads: dict) -> dict:
    """Versioned known repo patterns. Truncated or unknown paths prove no surface."""
    projects = {}
    for project_id, observed in git_reads.items():
        paths = [
            line.split(" | ", 1)[0].strip()
            for line in observed.get("diffstat", "").splitlines()
            if " | " in line and "..." not in line
        ]
        projects[project_id] = sorted(
            name
            for name, prefixes in _SURFACES.get(project_id, {}).items()
            if any(path.startswith(prefixes) for path in paths)
        )
    return {"version": 1, "projects": projects}


def _prose(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or _URL.search(value):
        raise ValueError("author prose must be nonempty and contain no URLs")
    clean = "\n".join(
        filter(None, (sanitise(line) for line in _CONTROL.sub("", value).splitlines()))
    )
    if not clean:
        raise ValueError("author prose is empty after sanitizing")
    return clean


def _fields(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        raise ValueError("unknown or missing report fields (author paths are not supported)")
    return value


def validate_morning_report(raw: str, brief: dict) -> tuple[dict, list[str]]:
    """Return bounded content with server-owned coverage, refs and verification.

    Structural grounding is not a semantic truth guarantee. The supervisor's
    procedure also requires cautious wording and evidence review.
    """
    if len(raw.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("morning report exceeds 32 KiB")
    authored = _fields(
        json.loads(raw), {"version", "summary", "projects"}, {"coverage", "global_facts", "omitted"}
    )
    if authored["version"] != 1 or isinstance(authored["version"], bool):
        raise ValueError("unsupported report version")
    result = build_fallback(brief)
    result["summary"] = _prose(authored["summary"])
    if "coverage" in authored and authored["coverage"] != result["coverage"]:
        raise ValueError("coverage is owned by the server")
    facts = {fact["key"]: fact for fact in brief["facts"]}
    used: set[str] = set()

    def refs(value, allowed):
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(ref, str) or ref not in allowed for ref in value)
        ):
            raise ValueError("unknown, wrong-project or wrong-section evidence reference")
        used.update(value)
        return list(dict.fromkeys(value))

    def items(values, allowed):
        if not isinstance(values, list):
            raise ValueError("report sections must be lists")
        output = []
        for value in values:
            item = _fields(
                value,
                {"text", "refs"},
                {"task_id", "late", "shipment", "prior_verification", "verification_label"},
            )
            evidence = refs(item["refs"], allowed)
            related = [facts[ref] for ref in evidence]
            output.append(
                {
                    "text": _prose(item["text"]),
                    "refs": evidence,
                    "task_id": related[0].get("task_id") if len(related) == 1 else None,
                    "late": any(fact.get("late", False) for fact in related),
                    "shipment": related[0]["detail"].get("shipment") if len(related) == 1 else None,
                    "prior_verification": "\n".join(
                        dict.fromkeys(
                            fact["detail"].get("verification", "")
                            for fact in related
                            if fact["detail"].get("verification")
                        )
                    ),
                    "verification_label": "agent-reported",
                }
            )
        return output

    if not isinstance(authored["projects"], list):
        raise ValueError("projects must be a list")
    by_id = {project["id"]: project for project in brief["projects"]}
    seen = set()
    projects = []
    check_count = 0
    for value in authored["projects"]:
        project = _fields(value, {"id", "landed", "pending", "failures", "manual_checks"}, {"name"})
        project_id = project["id"]
        if not isinstance(project_id, str) or project_id not in by_id or project_id in seen:
            raise ValueError("unknown or duplicate project")
        seen.add(project_id)
        original = by_id[project_id]
        rendered = {"id": project_id, "name": original.get("name", project_id)}
        for group in ("landed", "pending", "failures"):
            rendered[group] = items(project[group], original[group])
        if not isinstance(project["manual_checks"], list):
            raise ValueError("manual_checks must be a list")
        checks = []
        for value in project["manual_checks"]:
            check = _fields(
                value,
                {
                    "action",
                    "surface",
                    "expected_result",
                    "reason",
                    "refs",
                    "prior_verification",
                    "confidence",
                },
            )
            evidence = refs(check["refs"], original["landed"])
            if check["surface"] not in brief.get("surface_map", {}).get("projects", {}).get(
                project_id, []
            ):
                raise ValueError("manual check surface is unknown in the frozen change list")
            if check["confidence"] not in ("low", "medium", "high"):
                raise ValueError("confidence must be low, medium or high")
            checks.append(
                {
                    **{
                        field: _prose(check[field])
                        for field in (
                            "action",
                            "surface",
                            "expected_result",
                            "reason",
                            "confidence",
                        )
                    },
                    "refs": evidence,
                    "prior_verification": "\n".join(
                        dict.fromkeys(
                            facts[ref]["detail"].get("verification", "")
                            for ref in evidence
                            if facts[ref]["detail"].get("verification")
                        )
                    ),
                }
            )
        check_count += len(checks)
        rendered["manual_checks"] = checks
        projects.append(rendered)
    if seen != by_id.keys() or check_count > 10:
        raise ValueError("include each scoped project and at most 10 manual checks total")
    result["projects"] = projects
    if "global_facts" in authored:
        result["global_facts"] = items(
            authored["global_facts"],
            {ref for ref, fact in facts.items() if fact["project_id"] is None},
        )
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("morning report exceeds 32 KiB after rendering")
    return copy.deepcopy(result), sorted(used)
