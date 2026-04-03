"""Convert vibecop JSON output into agent-friendly summaries.

Groups findings by severity, truncates for context window limits,
and produces both detailed and summary output formats.
"""

from __future__ import annotations

# Severity display order and symbols
_SEVERITY_META = {
    "error": {"order": 0, "icon": "[ERROR]", "label": "Errors"},
    "warning": {"order": 1, "icon": "[WARN]", "label": "Warnings"},
    "info": {"order": 2, "icon": "[INFO]", "label": "Info"},
}

# Conservative limit to avoid blowing up agent context windows
_MAX_DETAIL_CHARS = 8000
_MAX_SUMMARY_CHARS = 2000


def format_findings(findings: list[dict], *, mode: str = "detailed") -> str:
    """Format findings into a human/agent-readable string.

    Args:
        findings: List of normalized finding dicts from runner.
        mode: ``"detailed"`` for full output, ``"summary"`` for compact.

    Returns:
        Formatted string suitable for agent consumption.
    """
    if not findings:
        return "No findings detected. Code looks clean."

    if mode == "summary":
        return _format_summary(findings)
    return _format_detailed(findings)


def _format_detailed(findings: list[dict]) -> str:
    """Full output with per-finding details, grouped by severity."""
    grouped = _group_by_severity(findings)
    parts: list[str] = []
    char_count = 0

    # Header with counts
    counts = _severity_counts(findings)
    header = _counts_header(counts)
    parts.append(header)
    char_count += len(header)

    for severity in ("error", "warning", "info"):
        group = grouped.get(severity, [])
        if not group:
            continue

        meta = _SEVERITY_META.get(severity, _SEVERITY_META["info"])
        section_header = f"\n--- {meta['label']} ({len(group)}) ---\n"
        parts.append(section_header)
        char_count += len(section_header)

        for finding in group:
            entry = _format_finding_entry(finding, meta["icon"])
            if char_count + len(entry) > _MAX_DETAIL_CHARS:
                remaining = sum(len(grouped.get(s, [])) for s in ("error", "warning", "info"))
                parts.append(
                    f"\n... truncated ({remaining - len(parts) + 2} more findings). "
                    "Re-run with higher max_findings to see all."
                )
                return "\n".join(parts)
            parts.append(entry)
            char_count += len(entry)

    return "\n".join(parts)


def _format_summary(findings: list[dict]) -> str:
    """Compact summary: counts + top findings only."""
    counts = _severity_counts(findings)
    parts: list[str] = [_counts_header(counts)]
    char_count = len(parts[0])

    # Show top errors, then top warnings
    for severity in ("error", "warning"):
        relevant = [f for f in findings if f.get("severity") == severity]
        if not relevant:
            continue

        meta = _SEVERITY_META[severity]
        for finding in relevant[:5]:  # Top 5 per severity in summary
            line = _format_finding_oneline(finding, meta["icon"])
            if char_count + len(line) > _MAX_SUMMARY_CHARS:
                parts.append("... (truncated)")
                return "\n".join(parts)
            parts.append(line)
            char_count += len(line)

    return "\n".join(parts)


def _format_finding_entry(finding: dict, icon: str) -> str:
    """Format a single finding with full details."""
    file_loc = finding.get("file", "unknown")
    line = finding.get("line", 0)
    if line:
        file_loc = f"{file_loc}:{line}"
    col = finding.get("column", 0)
    if col:
        file_loc = f"{file_loc}:{col}"

    detector = finding.get("detector", "unknown")
    message = finding.get("message", "")
    suggestion = finding.get("suggestion", "")

    lines = [f"{icon} {file_loc} [{detector}]"]
    if message:
        lines.append(f"  {message}")
    if suggestion:
        lines.append(f"  Fix: {suggestion}")

    return "\n".join(lines)


def _format_finding_oneline(finding: dict, icon: str) -> str:
    """Format a single finding as a compact one-liner."""
    file_loc = finding.get("file", "unknown")
    line = finding.get("line", 0)
    if line:
        file_loc = f"{file_loc}:{line}"

    detector = finding.get("detector", "")
    message = finding.get("message", "")

    # Truncate message for one-line display
    if len(message) > 80:
        message = message[:77] + "..."

    return f"{icon} {file_loc} [{detector}] {message}"


def _group_by_severity(findings: list[dict]) -> dict[str, list[dict]]:
    """Group findings by severity level."""
    grouped: dict[str, list[dict]] = {}
    for finding in findings:
        severity = finding.get("severity", "info")
        grouped.setdefault(severity, []).append(finding)
    return grouped


def _severity_counts(findings: list[dict]) -> dict[str, int]:
    """Count findings by severity."""
    counts: dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        severity = finding.get("severity", "info")
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _counts_header(counts: dict[str, int]) -> str:
    """Build a summary header line from severity counts."""
    total = sum(counts.values())
    parts = [f"Vibecop: {total} finding(s)"]
    for severity in ("error", "warning", "info"):
        count = counts.get(severity, 0)
        if count:
            parts.append(f"{count} {severity}(s)")
    return " | ".join(parts)
