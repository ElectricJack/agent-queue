"""Context mixin — project override loading."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class ContextMixin:
    """Project override loading mixed into Orchestrator."""

    async def _load_project_override(
        self,
        project_id: str,
        agent_type: str,
    ) -> str | None:
        """Load override ``.md`` file content for a project + agent type.

        Override files live at
        ``vault/projects/{project_id}/overrides/{agent_type}.md``.
        Returns the raw file content (including frontmatter — the caller
        is responsible for stripping it), or ``None`` if no override exists.

        See ``docs/specs/design/memory-scoping.md`` §5 for the override model.
        """
        override_path = os.path.join(
            self.config.vault_root,
            "projects",
            project_id,
            "overrides",
            f"{agent_type}.md",
        )
        if not os.path.isfile(override_path):
            return None
        try:
            with open(override_path, encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                logger.info(
                    "Loaded override for project=%s agent_type=%s from %s",
                    project_id,
                    agent_type,
                    override_path,
                )
                return content
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Failed to load override for project=%s agent_type=%s: %s",
                project_id,
                agent_type,
                exc,
            )
        return None
