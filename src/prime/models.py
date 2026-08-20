"""Dataclasses for the prime document (docs/specs/implementation/aq-surface.md §2).

``PrimeDocument`` is an ordered, immutable list of ten canonical sections
(design §5.2) plus enough metadata (task id, session id, render source,
timestamp) for consumers to reason about what they received.  The renderer
(``renderer.py``) is the only code that constructs one; this module is pure
data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Canonical section order (design §5.2, table order 1-10). Slots 7-8 (L1/L2
# memory) exist from day one, rendered empty while memory is paused
# (docs/specs/design/feature-pauses.md), so the memory comeback is a
# renderer change, not a protocol change.
SECTION_KEYS: tuple[str, ...] = (
    "role",
    "project_role",
    "task",
    "task_context",
    "workspaces",
    "messages",
    "l1_facts",
    "l2_context",
    "tool_guidance",
    "completion_protocol",
)

# Human-readable markdown headings for each section, used by
# ``PrimeDocument.to_markdown()``.
SECTION_TITLES: dict[str, str] = {
    "role": "Role",
    "project_role": "Project Role Override",
    "task": "Task",
    "task_context": "Task Context",
    "workspaces": "Workspaces",
    "messages": "Messages",
    "l1_facts": "Facts",
    "l2_context": "Topic Context",
    "tool_guidance": "Tool Guidance",
    "completion_protocol": "Completion Protocol",
}


@dataclass(frozen=True)
class PrimeSection:
    """One section of the prime document.

    ``body`` is an empty string when the section has nothing to render
    (e.g. no project override profile, memory paused) — the section still
    exists as a template variable for ``.aq/PRIME.md`` overrides (design
    §5.3), it's just omitted from the default ``to_markdown()`` assembly.
    """

    key: str  # one of SECTION_KEYS
    title: str
    body: str


@dataclass(frozen=True)
class PrimeDocument:
    """The full rendered prime document for one task/session.

    ``work_dir`` and ``branch`` are not part of the minimal shape sketched
    in the implementation spec's §2 pseudocode, but are required to satisfy
    the override template variables the spec itself documents in design
    §5.3 (``{{work_dir}}``, ``{{branch}}``, ``{{task.id}}``) — they are
    populated by the renderer from the task row / work-state lookup.
    """

    task_id: str
    session_id: str | None
    sections: tuple[PrimeSection, ...]
    source: str  # "default" | "override:.aq/PRIME.md"
    rendered_at: datetime
    work_dir: str | None = None
    branch: str | None = None
    # Set only when `source` is an override — the fully-substituted
    # override template body, which replaces the default assembly
    # entirely (design §5.3).
    override_markdown: str | None = field(default=None, repr=False)

    def to_markdown(self) -> str:
        """Render the document to a single markdown string.

        When ``source`` is an override, returns the substituted override
        body verbatim (it replaces default assembly entirely). Otherwise
        assembles non-empty sections in canonical order under ``## title``
        headings.
        """
        if self.override_markdown is not None:
            return self.override_markdown

        parts: list[str] = []
        for section in self.sections:
            if not section.body:
                continue
            parts.append(f"## {section.title}\n\n{section.body}".strip())
        if not parts:
            return ""
        return "\n\n".join(parts) + "\n"

    def section_vars(self) -> dict[str, str]:
        """Variables available to a ``.aq/PRIME.md`` override template.

        Keys are literal ``{{...}}`` tokens (design §5.3): one per section
        (``role``, ``task``, ``task_context``, ``workspaces``, ``messages``,
        ``tool_guidance``, ``completion_protocol``, plus the memory slots
        and ``project_role``), and the extra computed variables
        ``task.id``, ``work_dir``, ``branch``.
        """
        variables = {section.key: section.body for section in self.sections}
        variables["task.id"] = self.task_id
        variables["work_dir"] = self.work_dir or ""
        variables["branch"] = self.branch or ""
        return variables

    def tokens_est(self) -> int:
        """Crude chars/4 token estimate — same convention as prompt_builder.py."""
        return len(self.to_markdown()) // 4
