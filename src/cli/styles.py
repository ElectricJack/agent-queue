"""Rich theme, status colors, and visual constants for the CLI.

Centralizes all color and emoji mappings so the rest of the CLI
references styles by name rather than hard-coded ANSI codes.
"""

from __future__ import annotations

from rich.theme import Theme

# ---------------------------------------------------------------------------
# Status → Rich style mappings
# ---------------------------------------------------------------------------

STATUS_STYLES: dict[str, str] = {
    "DEFINED":                "dim white",
    "READY":                  "bold blue",
    "ASSIGNED":               "bold magenta",
    "IN_PROGRESS":            "bold yellow",
    "WAITING_INPUT":          "bold cyan",
    "PAUSED":                 "dim white",
    "VERIFYING":              "bold blue",
    "AWAITING_APPROVAL":      "bold bright_yellow",
    "AWAITING_PLAN_APPROVAL": "bold bright_yellow",
    "COMPLETED":              "bold green",
    "FAILED":                 "bold red",
    "BLOCKED":                "bold red",
}

STATUS_ICONS: dict[str, str] = {
    "DEFINED":                "⚪",
    "READY":                  "🔵",
    "ASSIGNED":               "📋",
    "IN_PROGRESS":            "🟡",
    "WAITING_INPUT":          "💬",
    "PAUSED":                 "⏸️",
    "VERIFYING":              "🔍",
    "AWAITING_APPROVAL":      "⏳",
    "AWAITING_PLAN_APPROVAL": "📋",
    "COMPLETED":              "🟢",
    "FAILED":                 "🔴",
    "BLOCKED":                "⛔",
}

PRIORITY_STYLES: dict[str, str] = {
    "critical": "bold red",     # priority >= 200
    "high":     "bold yellow",  # priority >= 150
    "normal":   "white",        # priority >= 50
    "low":      "dim white",    # priority < 50
}

AGENT_STATE_STYLES: dict[str, str] = {
    "IDLE":   "bold green",
    "BUSY":   "bold yellow",
    "PAUSED": "dim white",
    "ERROR":  "bold red",
}

AGENT_STATE_ICONS: dict[str, str] = {
    "IDLE":   "💤",
    "BUSY":   "⚡",
    "PAUSED": "⏸️",
    "ERROR":  "❌",
}

TASK_TYPE_ICONS: dict[str, str] = {
    "feature":  "✨",
    "bugfix":   "🐛",
    "refactor": "♻️",
    "test":     "🧪",
    "docs":     "📝",
    "chore":    "🔧",
    "research": "🔍",
    "plan":     "📋",
}


def priority_style(priority: int) -> str:
    """Return a Rich style string based on numeric priority."""
    if priority >= 200:
        return PRIORITY_STYLES["critical"]
    if priority >= 150:
        return PRIORITY_STYLES["high"]
    if priority >= 50:
        return PRIORITY_STYLES["normal"]
    return PRIORITY_STYLES["low"]


# ---------------------------------------------------------------------------
# Rich Theme (can be applied to Console for named style references)
# ---------------------------------------------------------------------------

AQ_THEME = Theme({
    "aq.header":    "bold bright_white",
    "aq.label":     "bold cyan",
    "aq.value":     "white",
    "aq.muted":     "dim white",
    "aq.success":   "bold green",
    "aq.error":     "bold red",
    "aq.warning":   "bold yellow",
    "aq.info":      "bold blue",
    "aq.id":        "bold bright_cyan",
    "aq.project":   "bold bright_magenta",
})
