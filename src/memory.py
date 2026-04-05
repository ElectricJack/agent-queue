"""Semantic memory manager for agent-queue using memsearch.

Provides per-project memory indexing and retrieval. Each project gets its own
Milvus collection and indexes markdown files from the workspace's memory/ and
notes/ directories.

Features (phases from the memory improvement plan):
- **Project Profile** — A living ``profile.md`` per project that captures
  synthesized knowledge (architecture, conventions, decisions, patterns).
- **Post-Task Revision** — After each completed task, an LLM call revises
  the project profile based on what the task learned.
- **Notes Integration** — Tasks can auto-generate categorized notes, and
  notes feed back into profile revision.
- **Memory Compaction** — Age-based lifecycle management that groups
  task memories into recent/medium/old tiers, LLM-summarizes medium-age
  memories into weekly digests, and removes old individual files.
- **Enhanced Context Delivery** — Tiered, prioritized context injection:
  profile > notes > recent tasks > semantic search results.

Optional dependency — when memsearch is not installed or memory is not
configured, all operations are no-ops.  All memsearch calls are wrapped in
try/except for resilience: a memory subsystem failure never blocks task
execution.

See ``specs/memory.md`` for the full memory system specification.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import time
from typing import Any

from src.config import MemoryConfig
from src.models import MemoryContext, ProjectFactsheet

logger = logging.getLogger(__name__)

try:
    from memsearch import MemSearch

    MEMSEARCH_AVAILABLE = True
except ImportError:
    MemSearch = None  # type: ignore[assignment,misc]
    MEMSEARCH_AVAILABLE = False


class MemoryManager:
    """Manages per-project MemSearch instances and memory operations.

    Each project gets its own Milvus collection (``aq_{project_id}_memory``)
    so memories are fully isolated between projects. Instances are created
    lazily on first access.

    Memory files are stored centrally under ``{data_dir}/memory/{project_id}/``
    to keep all persistent data under ``~/.agent-queue``.

    When memsearch is not installed or the config has ``enabled=False``,
    every public method degrades gracefully (returns empty lists, None, etc.)
    without raising exceptions.
    """

    def __init__(self, config: MemoryConfig, storage_root: str = "") -> None:
        self.config = config
        self._storage_root = os.path.expanduser(storage_root or "~/.agent-queue")
        self._instances: dict[str, Any] = {}  # project_id -> MemSearch
        self._watchers: dict[str, Any] = {}
        self._last_compact: dict[str, float] = {}  # project_id -> timestamp
        self._doc_file_mtimes: dict[str, float] = {}  # "workspace:relpath" -> mtime

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collection_name(self, project_id: str) -> str:
        """Deterministic, Milvus-safe collection name per project."""
        safe_id = project_id.replace("-", "_").replace(" ", "_")
        return f"aq_{safe_id}_memory"

    def _project_memory_dir(self, project_id: str) -> str:
        """Central memory storage directory for a project.

        Returns ``{data_dir}/memory/{project_id}/``.
        """
        return os.path.join(self._storage_root, "memory", project_id)

    def _profile_path(self, project_id: str) -> str:
        """Path to the project profile file.

        Returns ``{data_dir}/memory/{project_id}/profile.md``.
        """
        return os.path.join(self._project_memory_dir(project_id), "profile.md")

    def _notes_dir(self, project_id: str) -> str:
        """Path to the project notes directory."""
        return os.path.join(self._storage_root, "notes", project_id)

    def _memory_paths(self, project_id: str, workspace_path: str) -> list[str]:
        """Directories to index for a project.

        Always includes the central ``{data_dir}/memory/{project_id}/``
        directory (created if absent). Optionally includes the project's
        ``notes/`` directory when ``index_notes`` is enabled, and the
        workspace ``specs/`` and ``docs/`` directories when their respective
        config flags are enabled. This lets agents reference project
        specifications and documentation without duplicating files on disk —
        MemSearch indexes the originals in-place so updates are always current.
        """
        paths = [self._project_memory_dir(project_id)]
        if self.config.index_notes:
            notes_dir = self._notes_dir(project_id)
            if os.path.isdir(notes_dir):
                paths.append(notes_dir)
        if self.config.index_knowledge:
            knowledge_dir = self._knowledge_dir(project_id)
            if os.path.isdir(knowledge_dir):
                paths.append(knowledge_dir)
        # Include workspace specs/ and docs/ directories for zero-duplication
        # knowledge indexing — agents can answer questions about their own
        # architecture, configuration, and published documentation.
        if workspace_path:
            if self.config.index_specs:
                specs_dir = os.path.join(workspace_path, "specs")
                if os.path.isdir(specs_dir):
                    paths.append(specs_dir)
            if self.config.index_docs:
                docs_dir = os.path.join(workspace_path, "docs")
                if os.path.isdir(docs_dir):
                    paths.append(docs_dir)
        return paths

    async def _index_project_doc_files(self, instance: Any, workspace_path: str) -> None:
        """Index individual project documentation files (CLAUDE.md, README.md, etc.).

        These files live at the workspace root and aren't inside a directory
        that ``_memory_paths()`` covers, so they need explicit ``index_file()``
        calls.  We track modification times to avoid re-indexing unchanged files
        across multiple ``get_instance()`` calls within the same process.
        """
        if not self.config.index_project_docs or not workspace_path:
            return
        for rel_path in self.config.project_docs_files:
            full_path = os.path.join(workspace_path, rel_path)
            if not os.path.isfile(full_path):
                continue
            try:
                mtime = os.path.getmtime(full_path)
                key = f"{workspace_path}:{rel_path}"
                if self._doc_file_mtimes.get(key) == mtime:
                    continue  # unchanged since last index
                await instance.index_file(full_path)
                self._doc_file_mtimes[key] = mtime
            except Exception as e:
                logger.debug(f"Failed to index project doc {rel_path}: {e}")

    def _resolve_milvus_uri(self) -> str:
        """Expand ``~`` in Milvus Lite file paths and ensure parent dir exists."""
        uri = os.path.expanduser(self.config.milvus_uri)
        # For file-based Milvus Lite URIs, create the parent directory
        if not uri.startswith("http"):
            os.makedirs(os.path.dirname(uri), exist_ok=True)
        return uri

    # ------------------------------------------------------------------
    # Instance management
    # ------------------------------------------------------------------

    async def get_instance(self, project_id: str, workspace_path: str) -> Any | None:
        """Get or create a MemSearch instance for a project.

        Returns ``None`` when memsearch is unavailable or the subsystem is
        disabled — callers should treat ``None`` as "memory not available".
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            return None

        if project_id in self._instances:
            return self._instances[project_id]

        try:
            # Ensure the central memory/tasks directory exists for remember()
            memory_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
            os.makedirs(memory_dir, exist_ok=True)

            paths = self._memory_paths(project_id, workspace_path)

            # MemSearch.__init__ makes a synchronous HTTP call to the
            # embedding provider (e.g. Ollama embed) to discover the
            # vector dimension.  Run in a thread to avoid blocking the
            # asyncio event loop and starving the Discord gateway heartbeat.
            instance = await asyncio.to_thread(
                MemSearch,
                paths=paths,
                embedding_provider=self.config.embedding_provider,
                embedding_model=self.config.embedding_model or None,
                embedding_base_url=self.config.embedding_base_url or None,
                embedding_api_key=self.config.embedding_api_key or None,
                milvus_uri=self._resolve_milvus_uri(),
                milvus_token=self.config.milvus_token or None,
                collection=self._collection_name(project_id),
                max_chunk_size=self.config.max_chunk_size,
                overlap_lines=self.config.overlap_lines,
            )
            self._instances[project_id] = instance

            # Index individual root-level doc files (CLAUDE.md, README.md)
            await self._index_project_doc_files(instance, workspace_path)

            return instance
        except Exception as e:
            logger.error(f"Failed to create MemSearch instance for project {project_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Phase 1: Project Profile
    # ------------------------------------------------------------------

    async def get_profile(self, project_id: str) -> str | None:
        """Read and return the project profile content.

        Returns ``None`` if profiles are disabled or no profile exists yet.
        """
        if not self.config.profile_enabled:
            return None

        path = self._profile_path(project_id)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read profile for project {project_id}: {e}")
            return None

    async def update_profile(
        self, project_id: str, new_content: str, workspace_path: str = ""
    ) -> str | None:
        """Write updated profile content and re-index it.

        Truncates to ``profile_max_size`` if the content exceeds the limit.
        Returns the file path on success, ``None`` otherwise.
        """
        if not self.config.profile_enabled:
            return None

        # Enforce size limit
        max_size = self.config.profile_max_size
        if len(new_content) > max_size:
            # Truncate at last complete line within budget
            truncated = new_content[:max_size]
            last_newline = truncated.rfind("\n")
            if last_newline > 0:
                new_content = truncated[:last_newline] + "\n"
            else:
                new_content = truncated

        path = self._profile_path(project_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(new_content)
        except Exception as e:
            logger.warning(f"Failed to write profile for project {project_id}: {e}")
            return None

        # Re-index the profile file if we have an instance
        if workspace_path:
            instance = await self.get_instance(project_id, workspace_path)
            if instance:
                try:
                    await instance.index_file(path)
                except Exception as e:
                    logger.warning(f"Profile indexing failed for project {project_id}: {e}")

        return path

    # ------------------------------------------------------------------
    # Phase 1.5: Project Factsheet (Structured Metadata Layer)
    # ------------------------------------------------------------------

    def _factsheet_path(self, project_id: str) -> str:
        """Path to the project factsheet file.

        Returns ``{data_dir}/memory/{project_id}/factsheet.md``.
        """
        return os.path.join(self._project_memory_dir(project_id), "factsheet.md")

    def _parse_factsheet(self, raw: str) -> ProjectFactsheet:
        """Parse a factsheet file into a ``ProjectFactsheet`` dataclass.

        Splits on YAML frontmatter delimiters (``---``) and parses the
        YAML block.  Returns a ``ProjectFactsheet`` with empty defaults
        if parsing fails.
        """
        try:
            import yaml as _yaml
        except ImportError:
            # PyYAML not available — return raw content as body only
            logger.debug("PyYAML not installed; factsheet YAML parsing unavailable")
            return ProjectFactsheet(body_markdown=raw)

        # Split on frontmatter delimiters
        parts = raw.split("---", 2)
        if len(parts) < 3:
            # No valid frontmatter — treat entire content as markdown body
            return ProjectFactsheet(body_markdown=raw)

        yaml_text = parts[1]
        body = parts[2].strip()

        try:
            yaml_data = _yaml.safe_load(yaml_text)
            if not isinstance(yaml_data, dict):
                yaml_data = {}
        except Exception as e:
            logger.warning(f"Failed to parse factsheet YAML: {e}")
            yaml_data = {}

        return ProjectFactsheet(raw_yaml=yaml_data, body_markdown=body)

    def _serialize_factsheet(self, factsheet: ProjectFactsheet) -> str:
        """Serialize a ``ProjectFactsheet`` back to YAML-frontmatter + markdown."""
        try:
            import yaml as _yaml
        except ImportError:
            logger.debug("PyYAML not installed; cannot serialize factsheet")
            return factsheet.body_markdown

        yaml_text = _yaml.dump(
            factsheet.raw_yaml,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        body = factsheet.body_markdown
        return f"---\n{yaml_text}---\n\n{body}\n"

    async def read_factsheet(self, project_id: str) -> ProjectFactsheet | None:
        """Read and parse the project factsheet.

        Returns ``None`` if no factsheet exists yet.
        """
        path = self._factsheet_path(project_id)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                raw = f.read()
            return self._parse_factsheet(raw)
        except Exception as e:
            logger.warning(f"Failed to read factsheet for project {project_id}: {e}")
            return None

    async def read_factsheet_raw(self, project_id: str) -> str | None:
        """Read the raw factsheet content as a string.

        Returns ``None`` if no factsheet exists.  This is used for context
        injection where the full markdown is needed, not parsed fields.
        """
        path = self._factsheet_path(project_id)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read factsheet for project {project_id}: {e}")
            return None

    async def write_factsheet(
        self,
        project_id: str,
        factsheet: ProjectFactsheet,
        workspace_path: str = "",
    ) -> str | None:
        """Write a factsheet to disk and optionally re-index.

        Updates the ``last_updated`` timestamp automatically.
        Returns the file path on success, ``None`` on failure.
        """
        # Update the timestamp
        factsheet.raw_yaml["last_updated"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )

        content = self._serialize_factsheet(factsheet)
        path = self._factsheet_path(project_id)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write factsheet for project {project_id}: {e}")
            return None

        # Re-index the factsheet if we have a memsearch instance
        if workspace_path:
            instance = await self.get_instance(project_id, workspace_path)
            if instance:
                try:
                    await instance.index_file(path)
                except Exception as e:
                    logger.warning(f"Factsheet indexing failed for project {project_id}: {e}")

        logger.info(f"Factsheet written for project {project_id}")
        return path

    async def update_factsheet_field(
        self,
        project_id: str,
        dotted_key: str,
        value: Any,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> str | None:
        """Update a single field in the project factsheet.

        Creates the factsheet from the seed template if it doesn't exist yet.
        Uses dot notation for nested keys (e.g. ``"urls.github"``).

        Returns the file path on success, ``None`` on failure.
        """
        factsheet = await self.read_factsheet(project_id)
        if factsheet is None:
            # Bootstrap from seed template
            factsheet = await self._seed_factsheet(
                project_id,
                project_name=project_name,
                repo_url=repo_url,
            )

        factsheet.set_field(dotted_key, value)
        return await self.write_factsheet(project_id, factsheet, workspace_path)

    async def _seed_factsheet(
        self,
        project_id: str,
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> ProjectFactsheet:
        """Create a new factsheet from the seed template.

        Auto-populates ``urls.github`` from the project's ``repo_url``
        database field when available.
        """
        from src.prompts.memory_consolidation import FACTSHEET_SEED_TEMPLATE

        # Format the github_url as a YAML value (quoted string or null)
        github_url_yaml = f'"{repo_url}"' if repo_url else "null"

        raw = FACTSHEET_SEED_TEMPLATE.format(
            last_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            project_name=project_name or project_id,
            project_id=project_id,
            github_url=github_url_yaml,
        )

        return self._parse_factsheet(raw)

    async def ensure_factsheet(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> ProjectFactsheet:
        """Ensure a factsheet exists for the project, creating one if needed.

        This is the primary bootstrap entry point — call it during context
        building to guarantee a factsheet exists.  If the factsheet already
        exists, returns it as-is.

        Returns the (possibly newly created) ``ProjectFactsheet``.
        """
        existing = await self.read_factsheet(project_id)
        if existing is not None:
            return existing

        # Seed a new factsheet
        factsheet = await self._seed_factsheet(
            project_id,
            project_name=project_name,
            repo_url=repo_url,
        )
        await self.write_factsheet(project_id, factsheet, workspace_path)
        return factsheet

    # ------------------------------------------------------------------
    # Phase 2: Post-Task Profile Revision
    # ------------------------------------------------------------------

    def _build_revision_prompt(
        self,
        current_profile: str,
        task_summary: str,
        files_changed: str,
        *,
        task_title: str = "",
        task_type: str = "unknown",
        task_status: str = "completed",
        notes_section: str = "",
    ) -> tuple[str, str]:
        """Build the system and user prompts for a profile revision LLM call.

        Accepts the current profile content, a task summary, and a formatted
        list of changed files. Returns ``(system_prompt, user_prompt)`` ready
        for the LLM provider.
        """
        from src.prompts.memory_revision import (
            REVISION_SYSTEM_PROMPT,
            REVISION_USER_PROMPT,
        )

        system_prompt = REVISION_SYSTEM_PROMPT.format(
            max_size=self.config.profile_max_size,
        )
        user_prompt = REVISION_USER_PROMPT.format(
            current_profile=current_profile,
            task_title=task_title,
            task_type=task_type,
            task_status=task_status,
            task_summary=task_summary,
            files_changed=files_changed,
            notes_section=notes_section,
        )
        return system_prompt, user_prompt

    async def revise_profile(
        self,
        project_id: str,
        task: Any,
        output: Any,
        workspace_path: str,
    ) -> str | None:
        """Revise the project profile based on a completed task.

        Reads the current profile (or seeds from a template), calls an LLM
        to produce an updated version incorporating what the task learned,
        and writes the result back. Only called for COMPLETED tasks.

        Returns the updated profile content on success, ``None`` on failure
        or if revision is disabled.
        """
        if not self.config.revision_enabled or not self.config.profile_enabled:
            return None

        from src.prompts.memory_revision import PROFILE_SEED_TEMPLATE

        # Get current profile or seed
        current_profile = await self.get_profile(project_id)
        if not current_profile:
            current_profile = PROFILE_SEED_TEMPLATE

        # Extract task metadata for the revision prompt
        task_type = (
            task.task_type.value
            if (task.task_type and hasattr(task.task_type, "value"))
            else "unknown"
        )
        status = output.result.value if hasattr(output.result, "value") else str(output.result)
        summary = output.summary or "No summary available."
        files_changed = (
            "\n".join(f"- {f}" for f in (output.files_changed or [])) or "No files changed."
        )

        # Optionally include recent notes in revision context
        notes_section = ""
        if self.config.notes_inform_profile:
            notes_content = self._read_recent_notes(project_id, max_notes=5)
            if notes_content:
                notes_section = f"## Recent Project Notes\n{notes_content}\n\n"

        system_prompt, user_prompt = self._build_revision_prompt(
            current_profile=current_profile,
            task_summary=summary,
            files_changed=files_changed,
            task_title=task.title,
            task_type=task_type,
            task_status=status,
            notes_section=notes_section,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for profile revision")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=2048,
            )

            # Extract text from response
            new_profile = ""
            for block in response.content:
                if hasattr(block, "text"):
                    new_profile += block.text

            if not new_profile.strip():
                logger.warning("LLM returned empty profile revision")
                return None

            await self.update_profile(project_id, new_profile.strip(), workspace_path)
            logger.info(f"Profile revised for project {project_id} after task {task.id}")
            return new_profile.strip()

        except Exception as e:
            logger.warning(f"Profile revision failed for project {project_id}: {e}")
            return None

    def _get_revision_provider(self) -> Any | None:
        """Create a chat provider for revision/note-generation LLM calls.

        Uses revision_provider/revision_model config if set, otherwise
        falls back to the main chat_provider settings.
        """
        try:
            from src.chat_providers import create_chat_provider
            from src.config import ChatProviderConfig

            provider_name = self.config.revision_provider or "anthropic"
            model_name = self.config.revision_model or ""

            provider_config = ChatProviderConfig(
                provider=provider_name,
                model=model_name,
            )
            return create_chat_provider(provider_config)
        except Exception as e:
            logger.warning(f"Failed to create revision LLM provider: {e}")
            return None

    def _read_recent_notes(self, project_id: str, max_notes: int = 5) -> str:
        """Read the most recent notes for a project.

        Returns formatted markdown of the most recent notes, or empty string.
        """
        notes_dir = self._notes_dir(project_id)
        if not os.path.isdir(notes_dir):
            return ""

        try:
            note_files = sorted(
                glob.glob(os.path.join(notes_dir, "*.md")),
                key=os.path.getmtime,
                reverse=True,
            )[:max_notes]

            if not note_files:
                return ""

            sections = []
            for nf in note_files:
                with open(nf) as f:
                    content = f.read().strip()
                basename = os.path.basename(nf)
                sections.append(f"### {basename}\n{content}")

            return "\n\n".join(sections)
        except Exception as e:
            logger.warning(f"Failed to read notes for project {project_id}: {e}")
            return ""

    async def regenerate_profile(
        self,
        project_id: str,
        workspace_path: str,
    ) -> str | None:
        """Force-regenerate the project profile from task history.

        Reads all stored task memories and recent notes, then calls an LLM
        to synthesize a brand-new profile from scratch. This replaces the
        existing profile entirely.

        Returns the new profile content on success, ``None`` on failure.
        """
        if not self.config.profile_enabled:
            return None

        from src.prompts.memory_revision import (
            REGENERATION_SYSTEM_PROMPT,
            REGENERATION_USER_PROMPT,
        )

        # Gather all task memory files
        tasks_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
        task_summaries: list[str] = []
        if os.path.isdir(tasks_dir):
            task_files = sorted(
                glob.glob(os.path.join(tasks_dir, "*.md")),
                key=os.path.getmtime,
            )
            for tf in task_files:
                try:
                    with open(tf) as f:
                        content = f.read().strip()
                    if content:
                        task_summaries.append(content)
                except Exception:
                    pass

        if not task_summaries:
            logger.info(f"No task history for project {project_id}, cannot regenerate profile")
            return None

        # Optionally include recent notes
        notes_section = ""
        if self.config.notes_inform_profile:
            notes_content = self._read_recent_notes(project_id, max_notes=10)
            if notes_content:
                notes_section = f"## Recent Project Notes\n{notes_content}\n\n"

        system_prompt = REGENERATION_SYSTEM_PROMPT.format(
            max_size=self.config.profile_max_size,
        )
        user_prompt = REGENERATION_USER_PROMPT.format(
            task_count=len(task_summaries),
            task_summaries="\n\n---\n\n".join(task_summaries),
            notes_section=notes_section,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for profile regeneration")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=2048,
            )

            new_profile = ""
            for block in response.content:
                if hasattr(block, "text"):
                    new_profile += block.text

            if not new_profile.strip():
                logger.warning("LLM returned empty profile during regeneration")
                return None

            await self.update_profile(project_id, new_profile.strip(), workspace_path)
            logger.info(
                f"Profile regenerated for project {project_id} from {len(task_summaries)} tasks"
            )
            return new_profile.strip()

        except Exception as e:
            logger.warning(f"Profile regeneration failed for project {project_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Phase 3: Notes Integration
    # ------------------------------------------------------------------

    async def generate_task_notes(
        self, project_id: str, task: Any, output: Any, workspace_path: str
    ) -> list[str]:
        """Auto-generate notes from a completed task if warranted.

        Uses an LLM to assess whether the task produced noteworthy insights
        and, if so, creates categorized note files. Returns a list of note
        file paths created, or an empty list.
        """
        if not self.config.auto_generate_notes:
            return []

        from src.prompts.memory_revision import (
            NOTE_GENERATION_SYSTEM_PROMPT,
            NOTE_GENERATION_USER_PROMPT,
        )

        task_type = (
            task.task_type.value
            if (task.task_type and hasattr(task.task_type, "value"))
            else "unknown"
        )
        summary = output.summary or "No summary available."
        files_changed = (
            "\n".join(f"- {f}" for f in (output.files_changed or [])) or "No files changed."
        )

        user_prompt = NOTE_GENERATION_USER_PROMPT.format(
            task_title=task.title,
            task_type=task_type,
            project_id=project_id,
            task_summary=summary,
            files_changed=files_changed,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                return []

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=NOTE_GENERATION_SYSTEM_PROMPT,
                max_tokens=1024,
            )

            # Extract text and parse JSON
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse the JSON array from the response
            response_text = response_text.strip()
            # Handle markdown code fences
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                # Remove first and last lines (code fences)
                lines = [l for l in lines if not l.strip().startswith("```")]
                response_text = "\n".join(lines)

            notes = json.loads(response_text)
            if not isinstance(notes, list) or not notes:
                return []

            notes_dir = self._notes_dir(project_id)
            os.makedirs(notes_dir, exist_ok=True)

            created_paths: list[str] = []
            timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

            for note in notes:
                category = note.get("category", "general")
                slug = note.get("slug", "note")
                content = note.get("content", "")
                if not content:
                    continue

                filename = f"{category}-{slug}-{timestamp}.md"
                path = os.path.join(notes_dir, filename)

                with open(path, "w") as f:
                    f.write(content)
                created_paths.append(path)

                # Index the new note file
                instance = await self.get_instance(project_id, workspace_path)
                if instance:
                    try:
                        await instance.index_file(path)
                    except Exception as e:
                        logger.warning(f"Note indexing failed for {path}: {e}")

            if created_paths:
                logger.info(
                    f"Generated {len(created_paths)} note(s) for project {project_id} "
                    f"from task {task.id}"
                )

            return created_paths

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse note generation response: {e}")
            return []
        except Exception as e:
            logger.warning(f"Note generation failed for project {project_id}: {e}")
            return []

    async def promote_note(
        self,
        project_id: str,
        note_filename: str,
        note_content: str,
        workspace_path: str,
    ) -> str | None:
        """Incorporate a specific note's content into the project profile.

        Uses an LLM to integrate the note into the existing profile rather
        than simply appending. Returns the updated profile content on success,
        ``None`` on failure.
        """
        if not self.config.profile_enabled:
            return None

        from src.prompts.memory_revision import (
            NOTE_PROMOTION_SYSTEM_PROMPT,
            NOTE_PROMOTION_USER_PROMPT,
            PROFILE_SEED_TEMPLATE,
        )

        current_profile = await self.get_profile(project_id)
        if not current_profile:
            current_profile = PROFILE_SEED_TEMPLATE

        system_prompt = NOTE_PROMOTION_SYSTEM_PROMPT.format(
            max_size=self.config.profile_max_size,
        )
        user_prompt = NOTE_PROMOTION_USER_PROMPT.format(
            current_profile=current_profile,
            note_filename=note_filename,
            note_content=note_content,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for note promotion")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=2048,
            )

            new_profile = ""
            for block in response.content:
                if hasattr(block, "text"):
                    new_profile += block.text

            if not new_profile.strip():
                logger.warning("LLM returned empty profile during note promotion")
                return None

            await self.update_profile(project_id, new_profile.strip(), workspace_path)
            logger.info(f"Note '{note_filename}' promoted into profile for project {project_id}")
            return new_profile.strip()

        except Exception as e:
            logger.warning(f"Note promotion failed for project {project_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Phase 3.5: Post-Task Fact Extraction (Memory Consolidation)
    # ------------------------------------------------------------------

    def _staging_dir(self, project_id: str) -> str:
        """Path to the fact-staging directory for a project.

        Returns ``{data_dir}/memory/{project_id}/staging/``.
        """
        return os.path.join(self._project_memory_dir(project_id), "staging")

    async def extract_task_facts(
        self,
        project_id: str,
        task: Any,
        output: Any,
        workspace_path: str,
    ) -> str | None:
        """Extract structured facts from a completed task into a staging file.

        Uses an LLM to identify concrete facts (URLs, tech stack, decisions,
        conventions, architecture, config, contacts) from the task output and
        writes them to ``memory/{project_id}/staging/{task_id}.json``.

        These staging files are later consumed by the daily consolidation
        process (Phase 4) to update the project factsheet and knowledge base.

        Returns the staging file path on success, ``None`` on failure or if
        fact extraction is disabled.
        """
        if not self.config.fact_extraction_enabled:
            return None

        from src.prompts.memory_consolidation import (
            FACT_EXTRACTION_SYSTEM_PROMPT,
            FACT_EXTRACTION_USER_PROMPT,
        )

        # Extract task metadata
        task_type = (
            task.task_type.value
            if (task.task_type and hasattr(task.task_type, "value"))
            else "unknown"
        )
        summary = output.summary or "No summary available."
        files_changed = (
            "\n".join(f"- {f}" for f in (output.files_changed or [])) or "No files changed."
        )

        user_prompt = FACT_EXTRACTION_USER_PROMPT.format(
            task_id=task.id,
            task_title=task.title,
            task_type=task_type,
            project_id=project_id,
            task_summary=summary,
            files_changed=files_changed,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for fact extraction")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=FACT_EXTRACTION_SYSTEM_PROMPT,
                max_tokens=1024,
            )

            # Extract text from response
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse the JSON array from the response
            response_text = response_text.strip()
            # Handle markdown code fences
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                response_text = "\n".join(lines)

            facts = json.loads(response_text)
            if not isinstance(facts, list):
                logger.warning("Fact extraction returned non-array response")
                return None

            # Validate fact structure — keep only well-formed entries
            valid_categories = {
                "url", "tech_stack", "decision", "convention",
                "architecture", "config", "contact",
            }
            validated_facts = []
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                category = fact.get("category", "")
                key = fact.get("key", "")
                value = fact.get("value", "")
                if category in valid_categories and key and value:
                    validated_facts.append({
                        "category": category,
                        "key": key,
                        "value": value,
                    })

            # Build staging document
            staging_doc = {
                "task_id": task.id,
                "project_id": project_id,
                "task_title": task.title,
                "task_type": task_type,
                "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "facts": validated_facts,
            }

            # Write staging file
            staging_dir = self._staging_dir(project_id)
            os.makedirs(staging_dir, exist_ok=True)
            staging_path = os.path.join(staging_dir, f"{task.id}.json")

            with open(staging_path, "w") as f:
                json.dump(staging_doc, f, indent=2)

            if validated_facts:
                logger.info(
                    "Extracted %d fact(s) from task %s for project %s",
                    len(validated_facts),
                    task.id,
                    project_id,
                )
            else:
                logger.debug(
                    "No facts extracted from task %s for project %s",
                    task.id,
                    project_id,
                )

            return staging_path

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse fact extraction response: %s", e)
            return None
        except Exception as e:
            logger.warning("Fact extraction failed for project %s: %s", project_id, e)
            return None

    # ------------------------------------------------------------------
    # Phase 3.6: Knowledge Base Topic Files
    # ------------------------------------------------------------------

    def _knowledge_dir(self, project_id: str) -> str:
        """Path to the knowledge base directory for a project.

        Returns ``{data_dir}/memory/{project_id}/knowledge/``.
        """
        return os.path.join(self._project_memory_dir(project_id), "knowledge")

    def _knowledge_topic_path(self, project_id: str, topic: str) -> str:
        """Path to a specific knowledge topic file.

        Returns ``{data_dir}/memory/{project_id}/knowledge/{topic}.md``.
        """
        # Sanitize topic to prevent directory traversal
        safe_topic = topic.replace("/", "").replace("\\", "").replace("..", "")
        return os.path.join(self._knowledge_dir(project_id), f"{safe_topic}.md")

    async def read_knowledge_topic(self, project_id: str, topic: str) -> str | None:
        """Read a knowledge topic file for a project.

        Returns the file content as a string, or ``None`` if the topic file
        doesn't exist or knowledge indexing is disabled.
        """
        if not self.config.index_knowledge:
            return None

        path = self._knowledge_topic_path(project_id, topic)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read knowledge topic '{topic}' for project {project_id}: {e}")
            return None

    async def write_knowledge_topic(
        self,
        project_id: str,
        topic: str,
        content: str,
        workspace_path: str = "",
    ) -> str | None:
        """Write content to a knowledge topic file and optionally re-index.

        Creates the knowledge directory if it doesn't exist.
        Returns the file path on success, ``None`` on failure.
        """
        if not self.config.index_knowledge:
            return None

        # Validate topic against configured topics
        if topic not in self.config.knowledge_topics:
            logger.warning(
                f"Topic '{topic}' not in configured knowledge_topics for project {project_id}"
            )
            return None

        path = self._knowledge_topic_path(project_id, topic)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(
                f"Failed to write knowledge topic '{topic}' for project {project_id}: {e}"
            )
            return None

        # Re-index the topic file if we have a memsearch instance
        if workspace_path:
            instance = await self.get_instance(project_id, workspace_path)
            if instance:
                try:
                    await instance.index_file(path)
                except Exception as e:
                    logger.warning(
                        f"Knowledge topic indexing failed for '{topic}' "
                        f"in project {project_id}: {e}"
                    )

        logger.info(f"Knowledge topic '{topic}' written for project {project_id}")
        return path

    async def ensure_knowledge_topic(
        self,
        project_id: str,
        topic: str,
        workspace_path: str = "",
    ) -> str | None:
        """Ensure a knowledge topic file exists, seeding from template if needed.

        Returns the file path on success, ``None`` on failure or if the
        topic is not in the configured list.
        """
        if not self.config.index_knowledge:
            return None

        if topic not in self.config.knowledge_topics:
            return None

        path = self._knowledge_topic_path(project_id, topic)
        if os.path.isfile(path):
            return path

        # Seed from template
        from src.prompts.memory_consolidation import KNOWLEDGE_TOPIC_SEED_TEMPLATES

        template = KNOWLEDGE_TOPIC_SEED_TEMPLATES.get(topic)
        if not template:
            logger.warning(f"No seed template found for knowledge topic '{topic}'")
            return None

        content = template.format(
            last_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        return await self.write_knowledge_topic(project_id, topic, content, workspace_path)

    async def list_knowledge_topics(self, project_id: str) -> list[dict[str, Any]]:
        """List all configured knowledge topics and their status.

        Returns a list of dicts, each with:
        - ``topic``: the topic slug (e.g. ``"architecture"``)
        - ``exists``: whether the topic file exists on disk
        - ``path``: the full file path
        - ``size``: file size in bytes (0 if not exists)
        """
        result: list[dict[str, Any]] = []
        for topic in self.config.knowledge_topics:
            path = self._knowledge_topic_path(project_id, topic)
            exists = os.path.isfile(path)
            size = 0
            if exists:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    pass
            result.append({
                "topic": topic,
                "exists": exists,
                "path": path,
                "size": size,
            })
        return result

    # ------------------------------------------------------------------
    # Phase 4: Memory Compaction & Enhanced Context Delivery
    # ------------------------------------------------------------------

    async def compact(self, project_id: str, workspace_path: str) -> dict:
        """Compact old task memories into weekly digest files.

        Groups task memory files by age:
        - **Recent** (< ``compact_recent_days``): kept as-is (full detail).
        - **Medium** (``compact_recent_days`` .. ``compact_archive_days``):
          LLM-summarized into weekly digest files under ``digests/``.
        - **Old** (> ``compact_archive_days``): individual task files deleted
          after their content has been included in a digest.

        Returns a stats dict with counts of tasks inspected, digests
        created, and files removed.
        """
        tasks_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
        digests_dir = os.path.join(self._project_memory_dir(project_id), "digests")

        if not os.path.isdir(tasks_dir):
            return {
                "status": "no_tasks",
                "project_id": project_id,
                "tasks_inspected": 0,
                "digests_created": 0,
                "files_removed": 0,
            }

        now = time.time()
        recent_cutoff = now - (self.config.compact_recent_days * 86400)
        archive_cutoff = now - (self.config.compact_archive_days * 86400)

        # Classify task files by age tier
        recent: list[str] = []
        medium: list[str] = []  # candidates for digesting
        old: list[str] = []  # candidates for deletion after digesting

        task_files = glob.glob(os.path.join(tasks_dir, "*.md"))
        for tf in task_files:
            try:
                mtime = os.path.getmtime(tf)
            except OSError:
                continue
            if mtime >= recent_cutoff:
                recent.append(tf)
            elif mtime >= archive_cutoff:
                medium.append(tf)
            else:
                old.append(tf)

        # Combine medium + old as candidates for digesting (old files will
        # additionally be deleted after digesting).
        to_digest = medium + old
        digests_created = 0
        files_removed = 0

        if to_digest:
            # Group by ISO week for weekly digests
            week_buckets: dict[str, list[str]] = {}
            for tf in to_digest:
                try:
                    mtime = os.path.getmtime(tf)
                    dt = time.gmtime(mtime)
                    # ISO year-week key, e.g. "2026-W11"
                    import datetime as _dt

                    d = _dt.date(dt.tm_year, dt.tm_mon, dt.tm_mday)
                    iso_year, iso_week, _ = d.isocalendar()
                    week_key = f"{iso_year}-W{iso_week:02d}"
                except (OSError, ValueError):
                    week_key = "unknown"
                week_buckets.setdefault(week_key, []).append(tf)

            os.makedirs(digests_dir, exist_ok=True)
            old_set = set(old)

            for week_key, files in sorted(week_buckets.items()):
                digest_path = os.path.join(digests_dir, f"week-{week_key}.md")

                # Skip if digest already exists for this week
                if os.path.isfile(digest_path):
                    # Still remove old files even if digest already exists
                    for tf in files:
                        if tf in old_set:
                            try:
                                os.remove(tf)
                                files_removed += 1
                            except OSError as e:
                                logger.warning(f"Failed to remove old task file {tf}: {e}")
                    continue

                # Read contents for summarization
                contents: list[str] = []
                for tf in sorted(files, key=os.path.getmtime):
                    try:
                        with open(tf) as f:
                            contents.append(f.read().strip())
                    except OSError:
                        pass

                if not contents:
                    continue

                # LLM-summarize into a digest
                digest_text = await self._summarize_batch(contents, week_key)
                if digest_text:
                    try:
                        with open(digest_path, "w") as f:
                            f.write(digest_text)
                        digests_created += 1

                        # Index the new digest file
                        instance = await self.get_instance(project_id, workspace_path)
                        if instance:
                            try:
                                await instance.index_file(digest_path)
                            except Exception as e:
                                logger.warning(f"Digest indexing failed for {digest_path}: {e}")
                    except OSError as e:
                        logger.warning(f"Failed to write digest {digest_path}: {e}")
                        continue

                # Delete old (> archive_days) individual files now that they're digested
                for tf in files:
                    if tf in old_set:
                        try:
                            os.remove(tf)
                            files_removed += 1
                        except OSError as e:
                            logger.warning(f"Failed to remove old task file {tf}: {e}")

        self._last_compact[project_id] = now

        stats = {
            "status": "compacted",
            "project_id": project_id,
            "tasks_inspected": len(task_files),
            "recent_kept": len(recent),
            "medium_digested": len(medium),
            "old_removed": len(old),
            "digests_created": digests_created,
            "files_removed": files_removed,
        }
        logger.info(
            f"Memory compaction for {project_id}: "
            f"{len(task_files)} inspected, {digests_created} digests created, "
            f"{files_removed} files removed"
        )
        return stats

    async def _summarize_batch(self, task_memories: list[str], date_range: str = "") -> str:
        """LLM-summarize a batch of task memories into a digest.

        Returns the digest markdown text, or empty string on failure.
        """
        from src.prompts.memory_revision import (
            DIGEST_SYSTEM_PROMPT,
            DIGEST_USER_PROMPT,
        )

        combined = "\n\n---\n\n".join(task_memories)
        user_prompt = DIGEST_USER_PROMPT.format(
            task_count=len(task_memories),
            date_range=date_range or "unknown period",
            task_memories=combined,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for memory digest")
                return ""

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=DIGEST_SYSTEM_PROMPT,
                max_tokens=2048,
            )

            digest = ""
            for block in response.content:
                if hasattr(block, "text"):
                    digest += block.text

            return digest.strip()
        except Exception as e:
            logger.warning(f"Digest summarization failed: {e}")
            return ""

    async def build_context(
        self,
        project_id: str,
        task: Any,
        workspace_path: str,
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> MemoryContext:
        """Build a structured, tiered memory context for a task.

        Returns a ``MemoryContext`` with fields for each priority tier:
        0. Project factsheet (structured metadata — instant lookup)
        1. Project profile (always included, highest priority)
        1.5. Project documentation (CLAUDE.md — foundational context)
        2. Relevant notes (semantic search matched)
        3. Recent task memories (for continuity)
        4. Semantic search results (de-duplicated against above)

        The orchestrator uses this instead of the old flat recall approach.
        """
        ctx = MemoryContext()

        # Set the memory folder path so agents know where to find more context
        memory_dir = self._project_memory_dir(project_id)
        if os.path.isdir(memory_dir):
            ctx.memory_folder = memory_dir if memory_dir.endswith("/") else memory_dir + "/"

        # Tier 0: Project Factsheet (structured metadata for instant lookup)
        try:
            factsheet_raw = await self.read_factsheet_raw(project_id)
            if factsheet_raw:
                ctx.factsheet = factsheet_raw
        except Exception as e:
            logger.warning(f"Factsheet load failed for project {project_id}: {e}")

        # Tier 1: Project Profile
        if self.config.profile_enabled:
            profile = await self.get_profile(project_id)
            if profile:
                ctx.profile = profile

        # Tier 1.5: Project Documentation (CLAUDE.md, README.md)
        # Always included as foundational context so every agent knows the
        # project basics — conventions, architecture overview, and workflow.
        if self.config.index_project_docs and workspace_path:
            doc_parts: list[str] = []
            for rel_path in self.config.project_docs_files:
                full_path = os.path.join(workspace_path, rel_path)
                if os.path.isfile(full_path):
                    try:
                        with open(full_path) as f:
                            content = f.read().strip()
                        # Truncate to keep context budget reasonable
                        max_chars = 3000
                        if len(content) > max_chars:
                            content = content[:max_chars] + "\n\n[truncated]"
                        doc_parts.append(f"### {rel_path}\n{content}")
                    except Exception:
                        pass
            if doc_parts:
                ctx.project_docs = "\n\n".join(doc_parts)

        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return ctx

        query = f"{task.title} {task.description}"
        seen_sources: set[str] = set()

        # Tier 2: Relevant Notes (search notes directory specifically)
        if self.config.index_notes:
            try:
                notes_results = await instance.search(query, top_k=3)
                notes_lines = []
                if notes_results:
                    notes_dir = self._notes_dir(project_id)
                    for mem in notes_results:
                        source = mem.get("source", "")
                        # Only include results from the notes directory
                        if notes_dir and source.startswith(notes_dir):
                            seen_sources.add(source)
                            heading = mem.get("heading", "")
                            content = mem.get("content", "")
                            score = mem.get("score", 0)
                            entry = f"**{os.path.basename(source)}**"
                            if heading:
                                entry += f" — {heading}"
                            entry += f" (relevance: {score:.2f})\n{content}"
                            notes_lines.append(entry)
                if notes_lines:
                    ctx.notes = "\n\n".join(notes_lines)
            except Exception as e:
                logger.warning(f"Notes search failed for project {project_id}: {e}")

        # Tier 3: Recent Task Memories
        recent_count = self.config.context_include_recent
        if recent_count > 0:
            try:
                tasks_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
                if os.path.isdir(tasks_dir):
                    task_files = sorted(
                        glob.glob(os.path.join(tasks_dir, "*.md")),
                        key=os.path.getmtime,
                        reverse=True,
                    )[:recent_count]

                    recent_lines = []
                    for tf in task_files:
                        seen_sources.add(tf)
                        try:
                            with open(tf) as f:
                                content = f.read().strip()
                            # Only include the first ~500 chars for recent tasks
                            if len(content) > 500:
                                content = content[:500] + "..."
                            recent_lines.append(content)
                        except Exception:
                            pass
                    if recent_lines:
                        ctx.recent_tasks = "\n\n---\n\n".join(recent_lines)
            except Exception as e:
                logger.warning(f"Recent tasks read failed for project {project_id}: {e}")

        # Tier 4: Semantic Search Results (de-duplicated)
        try:
            k = self.config.recall_top_k
            results = await instance.search(query, top_k=k + len(seen_sources))
            if results:
                search_lines = []
                for mem in results:
                    source = mem.get("source", "")
                    # Skip profile and already-seen sources
                    if source in seen_sources:
                        continue
                    if source == self._profile_path(project_id):
                        continue
                    seen_sources.add(source)

                    heading = mem.get("heading", "")
                    content = mem.get("content", "")
                    score = mem.get("score", 0)
                    entry = f"*Source: {source}*"
                    if heading:
                        entry += f"\n*Section: {heading}*"
                    entry += f" (relevance: {score:.2f})\n{content}"
                    search_lines.append(entry)

                    if len(search_lines) >= k:
                        break

                if search_lines:
                    ctx.search_results = "\n\n".join(search_lines)
        except Exception as e:
            logger.warning(f"Semantic search failed for project {project_id}: {e}")

        return ctx

    # ------------------------------------------------------------------
    # Original Public API (preserved for compatibility)
    # ------------------------------------------------------------------

    async def recall(self, task: Any, workspace_path: str, top_k: int | None = None) -> list[dict]:
        """Search for memories relevant to *task*.

        Uses ``task.title + task.description`` as the hybrid search query so
        both semantic similarity and keyword overlap contribute to ranking.

        Returns an empty list on any error — memory recall failures must never
        block task execution.
        """
        if not self.config.auto_recall:
            return []

        instance = await self.get_instance(task.project_id, workspace_path)
        if not instance:
            return []

        k = top_k or self.config.recall_top_k
        query = f"{task.title} {task.description}"
        try:
            results = await instance.search(query, top_k=k)
            return results if results else []
        except Exception as e:
            logger.warning(f"Memory recall failed for task {task.id}: {e}")
            return []

    async def remember(self, task: Any, output: Any, workspace_path: str) -> str | None:
        """Save a task result as a structured markdown memory file.

        Writes the file to ``{data_dir}/memory/{project_id}/tasks/{task_id}.md``
        and indexes it via ``memsearch.index_file()``. Returns the file path
        on success, ``None`` otherwise.
        """
        if not self.config.auto_remember:
            return None

        instance = await self.get_instance(task.project_id, workspace_path)
        if not instance:
            return None

        memory_path = os.path.join(
            self._project_memory_dir(task.project_id), "tasks", f"{task.id}.md"
        )
        try:
            content = self._format_task_memory(task, output)
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)
            with open(memory_path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write memory file for task {task.id}: {e}")
            return None

        # Index the new file (non-fatal)
        try:
            await instance.index_file(memory_path)
        except Exception as e:
            logger.warning(f"Memory indexing failed for task {task.id}: {e}")

        return memory_path

    async def write_memory(
        self, project_id: str, workspace_path: str, key: str, content: str
    ) -> str | None:
        """Write an arbitrary key-value memory entry for a project.

        Stores the content as a markdown file at
        ``{data_dir}/memory/{project_id}/{key}.md`` and indexes it for
        semantic search. This is the agent-facing write API — use it for
        persistent state like timestamps, counters, or any structured data
        that should be retrievable via ``memory_search`` or ``read_memory``.

        Returns the file path on success, ``None`` otherwise.
        """
        memory_dir = self._project_memory_dir(project_id)
        # Sanitize key to be filesystem-safe
        safe_key = key.replace("/", "_").replace("\\", "_").replace("..", "_")
        if not safe_key.endswith(".md"):
            safe_key += ".md"
        memory_path = os.path.join(memory_dir, safe_key)

        try:
            os.makedirs(memory_dir, exist_ok=True)
            with open(memory_path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write memory file {safe_key}: {e}")
            return None

        # Index for semantic search (non-fatal)
        instance = await self.get_instance(project_id, workspace_path)
        if instance:
            try:
                await instance.index_file(memory_path)
            except Exception as e:
                logger.warning(f"Memory indexing failed for {safe_key}: {e}")

        return memory_path

    async def read_memory(self, project_id: str, key: str) -> str | None:
        """Read an arbitrary key-value memory entry for a project.

        Returns the file content, or ``None`` if the key doesn't exist.
        """
        memory_dir = self._project_memory_dir(project_id)
        safe_key = key.replace("/", "_").replace("\\", "_").replace("..", "_")
        if not safe_key.endswith(".md"):
            safe_key += ".md"
        memory_path = os.path.join(memory_dir, safe_key)

        if not os.path.isfile(memory_path):
            return None

        try:
            with open(memory_path, "r") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read memory file {safe_key}: {e}")
            return None

    async def search(
        self, project_id: str, workspace_path: str, query: str, top_k: int = 10
    ) -> list[dict]:
        """Ad-hoc semantic search across a project's memory.

        Exposed for command-handler and hook-engine usage.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return []

        try:
            results = await instance.search(query, top_k=top_k)
            return results if results else []
        except Exception as e:
            logger.warning(f"Memory search failed for project {project_id}: {e}")
            return []

    async def batch_search(
        self,
        project_id: str,
        workspace_path: str,
        queries: list[str],
        top_k: int = 10,
    ) -> dict[str, list[dict]]:
        """Run multiple semantic searches concurrently.

        Returns a dict mapping each query string to its results list.
        Individual query failures return empty lists without blocking others.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return {q: [] for q in queries}

        async def _single(q: str) -> tuple[str, list[dict]]:
            try:
                results = await instance.search(q, top_k=top_k)
                return (q, results if results else [])
            except Exception as e:
                logger.warning("Memory batch_search query %r failed: %s", q, e)
                return (q, [])

        pairs = await asyncio.gather(*[_single(q) for q in queries])
        return dict(pairs)

    async def reindex(self, project_id: str, workspace_path: str) -> int:
        """Force a full reindex of a project's memory.

        Returns the number of chunks indexed, or 0 on failure.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return 0

        try:
            result = await instance.index(force=True)
            return result if isinstance(result, int) else 0
        except Exception as e:
            logger.warning(f"Memory reindex failed for project {project_id}: {e}")
            return 0

    async def stats(self, project_id: str, workspace_path: str) -> dict:
        """Get memory statistics for a project.

        Returns a dict with at least ``enabled`` and ``available`` keys.
        Includes age-tier breakdown and digest count when task files exist.
        """
        if not self.config.enabled:
            return {"enabled": False, "available": MEMSEARCH_AVAILABLE}

        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return {
                "enabled": True,
                "available": MEMSEARCH_AVAILABLE,
                "error": "Failed to initialize MemSearch instance",
            }

        profile_exists = os.path.isfile(self._profile_path(project_id))
        last_compact = self._last_compact.get(project_id)

        # Age-tier breakdown of task memory files
        tasks_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
        digests_dir = os.path.join(self._project_memory_dir(project_id), "digests")

        task_count = 0
        recent_count = 0
        medium_count = 0
        old_count = 0

        if os.path.isdir(tasks_dir):
            now = time.time()
            recent_cutoff = now - (self.config.compact_recent_days * 86400)
            archive_cutoff = now - (self.config.compact_archive_days * 86400)

            for tf in glob.glob(os.path.join(tasks_dir, "*.md")):
                task_count += 1
                try:
                    mtime = os.path.getmtime(tf)
                except OSError:
                    continue
                if mtime >= recent_cutoff:
                    recent_count += 1
                elif mtime >= archive_cutoff:
                    medium_count += 1
                else:
                    old_count += 1

        digest_count = 0
        if os.path.isdir(digests_dir):
            digest_count = len(glob.glob(os.path.join(digests_dir, "*.md")))

        return {
            "enabled": True,
            "available": True,
            "collection": self._collection_name(project_id),
            "milvus_uri": self.config.milvus_uri,
            "embedding_provider": self.config.embedding_provider,
            "auto_recall": self.config.auto_recall,
            "auto_remember": self.config.auto_remember,
            "recall_top_k": self.config.recall_top_k,
            "profile_enabled": self.config.profile_enabled,
            "profile_exists": profile_exists,
            "revision_enabled": self.config.revision_enabled,
            "auto_generate_notes": self.config.auto_generate_notes,
            "compact_enabled": self.config.compact_enabled,
            "last_compact": last_compact,
            "task_memories": task_count,
            "task_memories_recent": recent_count,
            "task_memories_medium": medium_count,
            "task_memories_old": old_count,
            "digests": digest_count,
        }

    async def close(self) -> None:
        """Shutdown all MemSearch instances and file watchers."""
        for watcher in self._watchers.values():
            try:
                watcher.stop()
            except Exception as e:
                logger.debug(f"Error stopping memory watcher: {e}")
        for project_id, instance in self._instances.items():
            try:
                instance.close()
            except Exception as e:
                logger.debug(f"Error closing MemSearch for project {project_id}: {e}")
        self._instances.clear()
        self._watchers.clear()

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_task_memory(self, task: Any, output: Any) -> str:
        """Format a task result as a structured markdown memory file.

        The format follows the template from the design doc: a heading with
        the task ID and title, metadata block, summary, and files-changed
        section. This structure is optimised for memsearch's heading-based
        chunker.
        """
        date_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime())

        # Safely extract enum values
        status = output.result.value if hasattr(output.result, "value") else str(output.result)
        task_type = (
            task.task_type.value
            if (task.task_type and hasattr(task.task_type, "value"))
            else "unknown"
        )
        tokens = output.tokens_used if output.tokens_used else 0

        files_section = "No files changed."
        if output.files_changed:
            files_section = "\n".join(f"- {f}" for f in output.files_changed)

        summary = output.summary or "No summary available."

        return (
            f"# Task: {task.id} — {task.title}\n"
            f"\n"
            f"**Project:** {task.project_id} | **Type:** {task_type} | **Status:** {status}\n"
            f"**Date:** {date_str} | **Tokens:** {tokens:,}\n"
            f"\n"
            f"## Summary\n"
            f"{summary}\n"
            f"\n"
            f"## Files Changed\n"
            f"{files_section}\n"
        )
