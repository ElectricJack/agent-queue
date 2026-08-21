"""Per-section builders for the prime document (design §5.2).

Each ``build_*`` function returns one :class:`~src.prime.models.PrimeSection`
for its slot in the canonical order. Builders are independent — the
renderer (``renderer.py``) is the only place that assembles them into a
:class:`~src.prime.models.PrimeDocument`.

Role extraction reuses ``extract_section()`` from ``src/prompt_builder.py``
(pulls a ``## Heading`` body out of profile markdown) rather than
duplicating that parsing logic, per the implementation spec's explicit
instruction (§2).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.aq_uri import path_is_within
from src.prompt_builder import extract_section

from .models import SECTION_TITLES, PrimeSection

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _read_text(path: Path) -> str | None:
    """Best-effort markdown file read. Missing/unreadable files degrade to None."""
    try:
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.debug("prime: could not read %s", path, exc_info=True)
        return None


def _load_template(name: str) -> str:
    content = _read_text(_TEMPLATES_DIR / name)
    return content.strip() if content else ""


# ---------------------------------------------------------------------------
# Section 1 — L0 profile role
# ---------------------------------------------------------------------------


async def build_role_section(config: Any, profile_id: str | None) -> PrimeSection:
    """``vault/agent-types/<id>/profile.md`` ``## Role`` (design §5.2 #1)."""
    body = ""
    if profile_id:
        path = Path(config.vault_agent_types) / profile_id / "profile.md"
        content = _read_text(path)
        if content:
            body = extract_section(content, "Role") or ""
    return PrimeSection(key="role", title=SECTION_TITLES["role"], body=body)


# ---------------------------------------------------------------------------
# Section 2 — project override role
# ---------------------------------------------------------------------------


async def build_project_role_section(
    config: Any, profile_id: str | None, project_id: str | None
) -> PrimeSection:
    """``vault/projects/<pid>/agent-types/<id>/profile.md`` ``## Role`` (design §5.2 #2).

    Specificity wins (principle #6): this section supplements, it does not
    replace, section 1 — both render when both files define a Role.
    """
    body = ""
    if profile_id and project_id:
        path = Path(config.vault_projects) / project_id / "agent-types" / profile_id / "profile.md"
        content = _read_text(path)
        if content:
            body = extract_section(content, "Role") or ""
    return PrimeSection(key="project_role", title=SECTION_TITLES["project_role"], body=body)


# ---------------------------------------------------------------------------
# Section 3 — task pointer
# ---------------------------------------------------------------------------


def build_task_section(task: Any) -> PrimeSection:
    """Task id/title/status/description (design §5.2 #3).

    No dedicated acceptance-criteria query layer exists yet (``task_criteria``
    has no getter in ``src/database/queries/task_queries.py`` beyond the
    delete-on-task-delete path), so this section carries the task's
    description as the closest available stand-in — matching what
    ``task_show`` itself exposes today.
    """
    status = task.status
    status_value = getattr(status, "value", status)
    lines = [
        f"**id:** {task.id}",
        f"**title:** {task.title}",
        f"**status:** {status_value}",
    ]
    if task.description:
        lines.append("")
        lines.append(task.description.strip())
    return PrimeSection(key="task", title=SECTION_TITLES["task"], body="\n".join(lines).strip())


# ---------------------------------------------------------------------------
# Section 4 — task context (incl. spec_ref inlining + attachments)
# ---------------------------------------------------------------------------


def _render_spec_ref(config: Any, row: dict) -> str:
    """Resolve a ``type='spec_ref'`` task_context row and inline the section.

    ``content`` is JSON ``{"path": ..., "section": ...}`` (see
    docs/specs/implementation/supervisor-agent.md §8). ``path`` is resolved
    relative to the vault root when not absolute. Missing file / missing
    heading degrade to a visible "unresolved" note rather than raising —
    the producer (task_graph, a parallel lane) may not exist yet, and a
    dangling ref must never break prime delivery.

    **Containment is enforced here, not only at graph-validation time.** This
    function inlines the referenced file into another agent's prompt — the
    whole file when no ``section`` is given — so a row that reached the DB by
    any path other than a validated graph (direct ``task_context`` write, an
    older row, a future producer) must not become an arbitrary file read.
    The validator closes the same hole at authoring time
    (``src/task_graph/validator.resolve_spec_path_checked``); both ends are
    closed deliberately.
    """
    raw = row.get("content") or "{}"
    try:
        ref = json.loads(raw)
    except (TypeError, ValueError):
        return f"**spec_ref (unparseable content):** {raw}"

    ref_path = ref.get("path") or ""
    ref_section = ref.get("section") or ""
    vault_root = getattr(config, "vault_root", None)
    if not vault_root:
        return f"**spec_ref unresolved:** `{ref_path}` (no vault root configured)"

    resolved = Path(ref_path)
    if not resolved.is_absolute():
        resolved = Path(vault_root) / ref_path

    if not path_is_within(resolved, vault_root):
        return (
            f"**spec_ref refused:** `{ref_path}` resolves outside the vault "
            "- refusing to inline it"
        )

    content = _read_text(resolved)
    if content is None:
        return f"**spec_ref unresolved:** `{ref_path}` § {ref_section} (file not found)"

    body = extract_section(content, ref_section) if ref_section else content.strip()
    if not body:
        return f"**spec_ref unresolved:** `{ref_path}` § {ref_section} (heading not found)"
    return f"**{ref_path} § {ref_section}:**\n\n{body}"


async def build_task_context_section(db: Any, config: Any, task: Any) -> PrimeSection:
    """``task_context`` rows incl. inlined ``spec_ref`` + attachments (design §5.2 #4).

    ``type='handoff'`` rows are excluded here — they render in section 6
    (messages + handoff) instead.
    """
    rows = await db.get_task_contexts(task.id)
    blocks: list[str] = []
    for row in rows:
        ctype = row.get("type")
        if ctype == "handoff":
            continue
        if ctype == "spec_ref":
            blocks.append(_render_spec_ref(config, row))
            continue
        content = (row.get("content") or "").strip()
        if not content:
            continue
        label = row.get("label") or ctype or "context"
        blocks.append(f"**{label}:**\n{content}")

    attachments = getattr(task, "attachments", None) or []
    if attachments:
        blocks.append("**attachments:**\n" + "\n".join(f"- {p}" for p in attachments))

    body = "\n\n".join(b for b in blocks if b).strip()
    return PrimeSection(key="task_context", title=SECTION_TITLES["task_context"], body=body)


# ---------------------------------------------------------------------------
# Section 5 — workspaces
# ---------------------------------------------------------------------------


async def resolve_work_dir(db: Any, task: Any) -> str | None:
    """Best-effort work_dir lookup: ``task_metadata['work_dir']`` (set by ``task_set``)."""
    try:
        meta = await db.get_all_task_meta(task.id)
    except Exception:
        return None
    value = meta.get("work_dir")
    return value if isinstance(value, str) else None


async def build_workspaces_section(db: Any, task: Any, work_dir: str | None) -> PrimeSection:
    """work_dir / branch / pr_url / other attached workspace kinds (design §5.2 #5)."""
    lines: list[str] = []
    if work_dir:
        lines.append(f"**work_dir:** {work_dir}")
    if getattr(task, "branch_name", None):
        lines.append(f"**branch:** {task.branch_name}")
    if getattr(task, "pr_url", None):
        lines.append(f"**pr_url:** {task.pr_url}")

    fetch_reqs = getattr(db, "fetch_task_workspace_requirements", None)
    if fetch_reqs is not None:
        try:
            reqs = await fetch_reqs(task.id)
        except Exception:
            reqs = []
        kinds = sorted({r.kind_id for r in reqs}) if reqs else []
        if kinds:
            lines.append("**other attached kinds:** " + ", ".join(kinds))

    return PrimeSection(key="workspaces", title=SECTION_TITLES["workspaces"], body="\n".join(lines))


# ---------------------------------------------------------------------------
# Section 6 — pending messages + handoff note
# ---------------------------------------------------------------------------


async def build_messages_section(
    db: Any,
    task_id: str,
    *,
    config: Any = None,
    mark_delivered: bool = False,
    profile_id: str | None = None,
    session_name: str | None = None,
) -> PrimeSection:
    """Pending messages (design §5.2 #6) + latest ``task_context(type=handoff)``.

    Renders pending messages addressed to the priming context using the
    same envelope as the ``UserPromptSubmit`` inject hook
    (``[<id> from <kind>:<id>]`` header + body), so the agent sees one
    consistent format regardless of delivery path.  Fetches three inboxes
    — ``("task", task_id)``, ``("profile", profile_id)`` if known, and
    ``("session", session_name)`` if resolvable — then merges by id,
    de-dupes, and sorts by ``(priority asc, created_at asc)`` (the same
    order ``get_pending_messages`` uses natively).  When *mark_delivered*
    is true each rendered row is marked delivered via CAS with
    ``via="prime"`` — this is what makes prime a genuine delivery method
    rather than a peek.  Gated on ``config.messages.enabled``; when the
    flag is off the messages sub-section is skipped entirely (the handoff
    block still renders).
    """
    parts: list[str] = []

    messages_enabled = bool(
        getattr(getattr(config, "messages", None), "enabled", False)
    )
    if messages_enabled:
        inbox_queries: list[tuple[str, str]] = [("task", task_id)]
        if profile_id:
            inbox_queries.append(("profile", profile_id))
        if session_name:
            inbox_queries.append(("session", session_name))

        merged: dict[str, Any] = {}
        for kind, ident in inbox_queries:
            try:
                pending = await db.get_pending_messages(kind, ident, limit=50)
            except Exception:
                pending = []
            for msg in pending or []:
                merged.setdefault(msg.id, msg)

        ordered = sorted(
            merged.values(),
            key=lambda m: (getattr(m, "priority", 0), getattr(m, "created_at", 0) or 0),
        )
        for msg in ordered:
            header = f"[{msg.id} from {msg.from_kind}:{msg.from_id}]"
            if getattr(msg, "subject", None):
                header = f"{header} {msg.subject}"
            parts.append(f"{header}\n{msg.body}")
            if mark_delivered:
                try:
                    await db.mark_delivered(msg.id, via="prime")
                except Exception:
                    # Best-effort: an already-claimed row (nudge race) is
                    # legal and non-fatal; we still rendered it above.
                    pass

    rows = await db.get_task_contexts(task_id)
    handoff_rows = [r for r in rows if r.get("type") == "handoff"]
    if handoff_rows:
        # No timestamp column on task_context; DB read order (insertion
        # order under SQLite/Postgres without an explicit ORDER BY) is the
        # best available proxy for "latest" until that table gains one.
        latest = handoff_rows[-1]
        try:
            payload = json.loads(latest.get("content") or "{}")
        except (TypeError, ValueError):
            payload = {}
        subject = (payload.get("subject") or "").strip()
        detail = (payload.get("detail") or "").strip()
        block = "**handoff note:**"
        if subject:
            block += f"\nsubject: {subject}"
        if detail:
            block += f"\n{detail}"
        parts.append(block)

    return PrimeSection(key="messages", title=SECTION_TITLES["messages"], body="\n\n".join(parts).strip())


# ---------------------------------------------------------------------------
# Sections 7-8 — L1/L2 memory slots (empty while memory is paused)
# ---------------------------------------------------------------------------


def build_l1_facts_section(config: Any) -> PrimeSection:
    """L1 critical facts (design §5.2 #7) — always empty while memory is paused.

    docs/specs/design/feature-pauses.md: ``memory.enabled`` defaults False
    and the slot renders empty. The slot exists so the memory comeback is a
    renderer change, not a protocol change — when ``memory.enabled`` flips
    True, this function is where L1 rendering plugs back in.
    """
    if not getattr(getattr(config, "memory", None), "enabled", False):
        return PrimeSection(key="l1_facts", title=SECTION_TITLES["l1_facts"], body="")
    # Memory is enabled but the L1 renderer isn't wired here yet (S1 does
    # not implement memory content — only the paused-empty contract).
    return PrimeSection(key="l1_facts", title=SECTION_TITLES["l1_facts"], body="")


def build_l2_context_section(config: Any) -> PrimeSection:
    """L2 topic context (design §5.2 #8) — always empty while memory is paused."""
    if not getattr(getattr(config, "memory", None), "enabled", False):
        return PrimeSection(key="l2_context", title=SECTION_TITLES["l2_context"], body="")
    return PrimeSection(key="l2_context", title=SECTION_TITLES["l2_context"], body="")


# ---------------------------------------------------------------------------
# Section 9 — tool guidance (static template)
# ---------------------------------------------------------------------------


def build_tool_guidance_section() -> PrimeSection:
    body = _load_template("tool_guidance.md")
    return PrimeSection(key="tool_guidance", title=SECTION_TITLES["tool_guidance"], body=body)


# ---------------------------------------------------------------------------
# Section 10 — completion protocol (static template, task id substituted)
# ---------------------------------------------------------------------------


def build_completion_protocol_section(task_id: str) -> PrimeSection:
    body = _load_template("completion_protocol.md").replace("{task_id}", task_id)
    return PrimeSection(
        key="completion_protocol", title=SECTION_TITLES["completion_protocol"], body=body
    )
