"""Per-project ``.aq/PRIME.md`` override loading + substitution (design §5.3).

If ``<work_dir>/.aq/PRIME.md`` exists (committed to the project repo, so it
rides into every worktree), its body **replaces** the default rendered body
entirely.  It is a lightweight Mustache-style template: ``{{key}}`` tokens
are substituted from :meth:`PrimeDocument.section_vars`.  This is
deliberately not full Mustache (no sections/loops/partials) — just literal
token substitution, matching the flat variable set the design doc
enumerates (``{{role}}``, ``{{task}}``, ``{{task_context}}``,
``{{workspaces}}``, ``{{messages}}``, ``{{tool_guidance}}``,
``{{completion_protocol}}``, ``{{task.id}}``, ``{{work_dir}}``,
``{{branch}}``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PrimeDocument

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


def load_override(work_dir: str | Path | None) -> str | None:
    """Return the raw contents of ``<work_dir>/.aq/PRIME.md``, or ``None``.

    ``None`` covers both "no work_dir known" and "file does not exist" —
    callers treat both as "use the default rendering" (design §5.3).
    """
    if not work_dir:
        return None
    path = Path(work_dir) / ".aq" / "PRIME.md"
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def apply_override(template: str, doc: "PrimeDocument") -> str:
    """Substitute ``{{var}}`` tokens in *template* from ``doc.section_vars()``.

    Unknown variables resolve to an empty string rather than raising —
    an override that references a not-yet-existing slot (e.g. a future
    memory tier) degrades gracefully instead of crashing prime delivery.
    """
    variables = doc.section_vars()

    def _substitute(match: re.Match[str]) -> str:
        return variables.get(match.group(1), "")

    return _VAR_RE.sub(_substitute, template)
