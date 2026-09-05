"""``PrimeRenderer`` — pure assembly of the ten-section prime document.

Reads profile markdown, task rows, ``task_context`` rows, attachments, and
workspace state; produces an ordered :class:`~src.prime.models.PrimeDocument`.
No LLM calls, no writes (design §5.1).

Two consumers share this renderer (design §5.1):

1. ``_cmd_prime`` (``src/commands/surface_commands.py``) — the CommandHandler
   command backing ``aq prime`` and the task-scope MCP tool.
2. Session-runtime's prompt-file writer — imports :class:`PrimeRenderer`
   directly (same process, no HTTP hop) and writes ``doc.to_markdown()`` to
   ``<work_dir>/.aq/prompt.md`` before the harness launches, then sets
   ``AQ_STARTUP_PROMPT_DELIVERED=1`` in the session env so the SessionStart
   hook (``aq prime --hook-json``) doesn't re-deliver the same body — see
   ``hook_envelopes.suppressed()``. Session-runtime has not landed yet, so
   that write path is not implemented here; this module only documents the
   handshake it will use.

``db`` is untyped (``Any``) rather than importing a concrete backend class:
the concrete type is whatever ``src.database.create_database()`` returns
(a ``DatabaseBackend`` Protocol instance), and this module only calls a
handful of its methods (``get_task``, ``get_task_contexts``,
``get_all_task_meta``, ``fetch_task_workspace_requirements`` if present).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import sections as _sections
from .models import PrimeDocument
from .overrides import apply_override, load_override


class PrimeRenderer:
    """Assembles a :class:`PrimeDocument` for one task (design §2)."""

    def __init__(self, db: Any, config: Any) -> None:
        self.db = db
        self.config = config

    async def render_for_task(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        work_dir: str | None = None,
        mark_messages_delivered: bool = False,
    ) -> PrimeDocument:
        """Render the full prime document for *task_id*.

        Raises ``ValueError`` for a missing/unknown task_id — callers
        (``_cmd_prime``) translate that into ``{"error": "..."}``.
        """
        if not task_id:
            raise ValueError("task_id is required")

        task = await self.db.get_task(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not found")

        # Use the active session for workspace and inbox context. Task metadata
        # can point at a slot from an earlier attempt that has since been reused.
        session_name: str | None = None
        session_lifecycle: str | None = None
        sess = None
        live_states = ("starting", "running", "draining")
        get_session = getattr(self.db, "get_session", None)
        if session_id and get_session is not None:
            try:
                candidate = await get_session(session_id)
            except Exception:
                candidate = None
            if (
                candidate is not None
                and candidate.task_id == task_id
                and candidate.state in live_states
            ):
                sess = candidate
        get_session_for_task = getattr(self.db, "get_session_for_task", None)
        if sess is None and get_session_for_task is not None:
            try:
                sess = await get_session_for_task(task_id)
            except Exception:
                sess = None
        if sess is not None:
            session_name = getattr(sess, "name", None)
            session_lifecycle = getattr(sess, "lifecycle", None)
        session_profile_id = (
            getattr(sess, "profile_id", None)
            if getattr(sess, "state", None) in live_states
            else None
        )
        session_work_dir = (
            getattr(sess, "work_dir", None)
            if getattr(sess, "state", None) in live_states
            else None
        )
        effective_work_dir = (
            work_dir or session_work_dir or await _sections.resolve_work_dir(self.db, task)
        )
        effective_profile_id = session_profile_id or task.profile_id
        if not effective_profile_id:
            project = await self.db.get_project(task.project_id)
            effective_profile_id = getattr(project, "default_profile_id", None)

        allow_emergent_work = await _sections.profile_allows_create_task(
            self.db, effective_profile_id
        )

        section_tuple = (
            await _sections.build_role_section(self.config, effective_profile_id),
            await _sections.build_project_role_section(
                self.config, effective_profile_id, task.project_id
            ),
            _sections.build_task_section(
                task,
                review_deliverables=await _sections.build_review_deliverable_summary(self.db, task),
                integration_delivery=await _sections.build_integration_delivery_summary(
                    self.db, task
                ),
            ),
            await _sections.build_task_context_section(self.db, self.config, task),
            await _sections.build_workspaces_section(self.db, task, effective_work_dir),
            await _sections.build_messages_section(
                self.db,
                task_id,
                config=self.config,
                mark_delivered=mark_messages_delivered,
                profile_id=effective_profile_id,
                session_name=session_name,
            ),
            _sections.build_l1_facts_section(self.config),
            _sections.build_l2_context_section(self.config),
            _sections.build_tool_guidance_section(),
            _sections.build_completion_protocol_section(
                task_id,
                lifecycle=session_lifecycle,
                allow_emergent_work=allow_emergent_work,
            ),
        )

        doc = PrimeDocument(
            task_id=task_id,
            session_id=session_id,
            sections=section_tuple,
            source="default",
            rendered_at=datetime.now(timezone.utc),
            work_dir=effective_work_dir,
            branch=task.branch_name,
        )

        override_template = load_override(effective_work_dir)
        if override_template:
            doc = PrimeDocument(
                task_id=doc.task_id,
                session_id=doc.session_id,
                sections=doc.sections,
                source="override:.aq/PRIME.md",
                rendered_at=doc.rendered_at,
                work_dir=doc.work_dir,
                branch=doc.branch,
                override_markdown=apply_override(override_template, doc),
            )

        return doc
