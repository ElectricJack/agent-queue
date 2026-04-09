"""LLM-powered compiler that turns playbook markdown into executable JSON graphs.

Reads a playbook ``.md`` file (natural language body + YAML frontmatter),
invokes an LLM with the content and the target JSON Schema, validates the
LLM's output, and returns a :class:`~src.playbook_models.CompiledPlaybook`.

The compiler is a *one-shot translation* — it runs once per markdown edit,
not per playbook execution.  The resulting JSON graph is what the runtime
executor operates on.

See ``docs/specs/design/playbooks.md`` Section 4 for the specification.

Typical usage::

    from src.playbook_compiler import PlaybookCompiler
    from src.chat_providers import create_chat_provider

    provider = create_chat_provider(config)
    compiler = PlaybookCompiler(provider)

    result = await compiler.compile(markdown_content)
    if result.success:
        compiled = result.playbook  # CompiledPlaybook instance
    else:
        print(result.errors)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

from src.playbook_models import CompiledPlaybook, generate_json_schema

if TYPE_CHECKING:
    from src.chat_providers.base import ChatProvider

logger = logging.getLogger(__name__)

# Maximum number of LLM retry attempts when the first response fails
# validation.  Each retry includes the validation errors as feedback.
MAX_RETRIES = 2

# Default max_tokens for the compilation LLM call.  Playbook graphs are
# typically compact (under 2k tokens) but we leave headroom for complex
# playbooks with many nodes.
DEFAULT_MAX_TOKENS = 4096


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CompilationResult:
    """Outcome of a playbook compilation attempt.

    Attributes:
        success: ``True`` if compilation produced a valid playbook.
        playbook: The compiled playbook, or ``None`` on failure.
        errors: Human-readable error strings (empty on success).
        source_hash: SHA-256 hash (16 hex chars) of the source markdown.
        raw_json: The raw JSON dict extracted from the LLM response,
            before dataclass conversion.  Useful for debugging.
        retries_used: How many retry rounds were needed (0 = first attempt).
        skipped: ``True`` if compilation was skipped because the source
            markdown has not changed since the last successful compilation
            (source hash matches the active compiled version).
    """

    success: bool
    playbook: CompiledPlaybook | None = None
    errors: list[str] = field(default_factory=list)
    source_hash: str = ""
    raw_json: dict[str, Any] | None = None
    retries_used: int = 0
    skipped: bool = False


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


class PlaybookCompiler:
    """Compiles playbook markdown into a validated :class:`CompiledPlaybook`.

    The compiler follows the pipeline described in the spec §4:

    1. Parse YAML frontmatter (``id``, ``triggers``, ``scope``, etc.)
    2. Compute a content hash for change detection
    3. Invoke an LLM with the markdown body + JSON Schema
    4. Extract JSON from the LLM response
    5. Merge frontmatter fields into the JSON (frontmatter is authoritative)
    6. Validate the result structurally
    7. Return :class:`CompilationResult`

    Parameters
    ----------
    provider:
        The :class:`~src.chat_providers.base.ChatProvider` used for LLM calls.
    max_retries:
        Maximum number of retry attempts when the LLM produces invalid
        output.  Each retry feeds the validation errors back to the LLM.
    max_tokens:
        Token budget for each LLM compilation call.
    """

    def __init__(
        self,
        provider: ChatProvider,
        *,
        max_retries: int = MAX_RETRIES,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._provider = provider
        self._max_retries = max_retries
        self._max_tokens = max_tokens
        self._schema = generate_json_schema()

    # -- public API ----------------------------------------------------------

    async def compile(
        self,
        markdown: str,
        *,
        existing_version: int = 0,
    ) -> CompilationResult:
        """Compile a playbook markdown file into a :class:`CompiledPlaybook`.

        Parameters
        ----------
        markdown:
            Raw content of the playbook ``.md`` file, including YAML
            frontmatter.
        existing_version:
            The version number of the currently compiled playbook (if any).
            The new version will be ``existing_version + 1``.

        Returns
        -------
        CompilationResult
            Contains either a valid ``playbook`` or a list of ``errors``.
        """
        # 1. Parse frontmatter
        frontmatter, body = self._parse_frontmatter(markdown)

        # Validate required frontmatter fields
        fm_errors = self._validate_frontmatter(frontmatter)
        if fm_errors:
            return CompilationResult(success=False, errors=fm_errors)

        # 2. Compute source hash
        source_hash = self._compute_source_hash(markdown)

        # 3. Build version
        version = existing_version + 1

        # 4. Invoke LLM (with retries)
        system_prompt = self._build_system_prompt()
        user_message = self._build_user_message(frontmatter, body)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

        last_errors: list[str] = []
        raw_json: dict[str, Any] | None = None
        raw_response_text: str = ""
        retries_used = 0

        for attempt in range(1 + self._max_retries):
            if attempt > 0:
                retries_used = attempt
                # Feed validation errors back as a follow-up message
                feedback = self._build_retry_message(last_errors)
                messages.append({"role": "assistant", "content": raw_response_text})
                messages.append({"role": "user", "content": feedback})

            try:
                response = await self._provider.create_message(
                    messages=messages,
                    system=system_prompt,
                    max_tokens=self._max_tokens,
                )
            except Exception as exc:
                logger.error(
                    "LLM call failed during playbook compilation (attempt %d): %s",
                    attempt + 1,
                    exc,
                )
                return CompilationResult(
                    success=False,
                    errors=[f"LLM call failed: {exc}"],
                    source_hash=source_hash,
                )

            # 5. Extract JSON from LLM response
            raw_response_text = "\n".join(response.text_parts)
            raw_json = self._extract_json(raw_response_text)

            if raw_json is None:
                last_errors = [
                    "Could not extract valid JSON from LLM response. "
                    "Expected a JSON object in a fenced code block or as bare JSON."
                ]
                logger.warning(
                    "Playbook compilation attempt %d: no JSON extracted from response",
                    attempt + 1,
                )
                continue

            # 6. Merge authoritative frontmatter fields
            raw_json = self._merge_frontmatter(raw_json, frontmatter, source_hash, version)

            # 7. Deserialize into CompiledPlaybook
            try:
                playbook = CompiledPlaybook.from_dict(raw_json)
            except Exception as exc:
                last_errors = [f"Failed to deserialize compiled JSON: {exc}"]
                logger.warning(
                    "Playbook compilation attempt %d: deserialization failed: %s",
                    attempt + 1,
                    exc,
                )
                continue

            # 8. Validate structure
            validation_errors = playbook.validate()
            if validation_errors:
                last_errors = validation_errors
                logger.warning(
                    "Playbook compilation attempt %d: %d validation error(s): %s",
                    attempt + 1,
                    len(validation_errors),
                    "; ".join(validation_errors),
                )
                continue

            # Success!
            logger.info(
                "Playbook '%s' compiled successfully (version=%d, hash=%s, nodes=%d, retries=%d)",
                playbook.id,
                playbook.version,
                source_hash,
                len(playbook.nodes),
                retries_used,
            )
            return CompilationResult(
                success=True,
                playbook=playbook,
                source_hash=source_hash,
                raw_json=raw_json,
                retries_used=retries_used,
            )

        # All attempts exhausted
        logger.error(
            "Playbook compilation failed after %d attempt(s) for '%s': %s",
            1 + self._max_retries,
            frontmatter.get("id", "<unknown>"),
            "; ".join(last_errors),
        )
        return CompilationResult(
            success=False,
            errors=last_errors,
            source_hash=source_hash,
            raw_json=raw_json,
            retries_used=retries_used,
        )

    # -- frontmatter ---------------------------------------------------------

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        """Split YAML frontmatter from the markdown body.

        Returns ``(metadata_dict, body_string)``.  Returns ``({}, content)``
        when no valid frontmatter is found.
        """
        if not content.startswith("---"):
            return {}, content
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return {}, content
        return meta, parts[2]

    @staticmethod
    def _validate_frontmatter(frontmatter: dict[str, Any]) -> list[str]:
        """Check that required frontmatter fields are present and valid.

        Returns a list of error strings (empty = valid).
        """
        errors: list[str] = []
        if not frontmatter:
            errors.append("Missing YAML frontmatter (file must start with '---')")
            return errors

        if not frontmatter.get("id"):
            errors.append("Frontmatter missing required field: 'id'")

        triggers = frontmatter.get("triggers")
        if not triggers:
            errors.append("Frontmatter missing required field: 'triggers'")
        elif not isinstance(triggers, list):
            errors.append("Frontmatter 'triggers' must be a list")
        elif not all(isinstance(t, str) and t for t in triggers):
            errors.append("Frontmatter 'triggers' must be a list of non-empty strings")

        if not frontmatter.get("scope"):
            errors.append("Frontmatter missing required field: 'scope'")
        else:
            scope = frontmatter["scope"]
            if scope not in ("system", "project") and not scope.startswith("agent-type:"):
                errors.append(
                    f"Frontmatter 'scope' must be 'system', 'project', or "
                    f"'agent-type:{{type}}', got: '{scope}'"
                )

        # 'enabled' is optional, default True
        if "enabled" in frontmatter:
            enabled = frontmatter["enabled"]
            if not isinstance(enabled, bool):
                errors.append("Frontmatter 'enabled' must be a boolean")

        return errors

    # -- hashing -------------------------------------------------------------

    @staticmethod
    def _normalize_content(content: str) -> str:
        """Normalize playbook markdown for stable hashing.

        Strips cosmetic differences that don't affect the compiled output:

        - **YAML frontmatter comments** (``# ...`` lines) — removed by parsing
          the YAML and re-serializing with sorted keys.
        - **HTML/Markdown comments** (``<!-- ... -->``) — stripped from the body.
        - **Trailing whitespace** on each line.
        - **Multiple consecutive blank lines** collapsed to one.
        - **Leading/trailing blank lines** trimmed.

        The result is a canonical string used only for hashing — it is never
        displayed or persisted.
        """
        frontmatter, body = PlaybookCompiler._parse_frontmatter(content)

        # Canonical frontmatter: sorted keys, no comments
        if frontmatter:
            fm_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True).strip()
        else:
            fm_str = ""

        # Strip HTML comments from body
        body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)

        # Normalize whitespace
        lines = [line.rstrip() for line in body.splitlines()]
        normalized: list[str] = []
        prev_blank = False
        for line in lines:
            if not line:
                if not prev_blank:
                    normalized.append("")
                prev_blank = True
            else:
                normalized.append(line)
                prev_blank = False
        body = "\n".join(normalized).strip()

        return f"{fm_str}\n---\n{body}"

    @staticmethod
    def _compute_source_hash(content: str) -> str:
        """Compute a stable SHA-256 hash (16 hex chars) of normalized markdown.

        The hash covers frontmatter values (parsed, sorted, comment-free) and
        the body with HTML comments stripped and whitespace normalized.  This
        ensures cosmetic-only edits (extra blank lines, trailing spaces, YAML
        or HTML comments) do **not** change the hash.
        """
        normalized = PlaybookCompiler._normalize_content(content)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    # -- prompt construction -------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Build the system prompt for the compilation LLM call."""
        schema_json = json.dumps(self._schema, indent=2)
        return (
            "You are a playbook compiler. Your task is to read a playbook "
            "written in natural language markdown and compile it into a "
            "structured JSON workflow graph.\n\n"
            "The output must be a single JSON object conforming EXACTLY to "
            "the following JSON Schema:\n\n"
            f"```json\n{schema_json}\n```\n\n"
            "## Rules\n\n"
            "1. Output ONLY a single JSON object inside a fenced code block "
            "(```json ... ```). No other text, explanation, or commentary.\n"
            "2. The JSON must conform to the schema above. Do not add extra "
            "fields.\n"
            '3. Every playbook must have exactly ONE node with `"entry": true`.\n'
            "4. Every playbook must have at least ONE node with "
            '`"terminal": true`.\n'
            '5. Non-terminal nodes MUST have a `"prompt"` field — a focused '
            "instruction describing what the LLM should do at that step.\n"
            '6. Each non-terminal node must have either `"transitions"` (a list '
            'of conditional edges) OR `"goto"` (an unconditional next node), '
            "but NOT both.\n"
            '7. Every transition must have either `"when"` (a natural language '
            'condition) or `"otherwise": true` (the fallback), plus a `"goto"` '
            "target.\n"
            "8. Node IDs should be short, descriptive, snake_case identifiers.\n"
            "9. Prompts in nodes should be clear, actionable instructions — "
            "they will be executed by an LLM at runtime.\n"
            "10. Do NOT include the `id`, `version`, `source_hash`, `triggers`, "
            "or `scope` fields in your output — those are injected from the "
            "playbook's YAML frontmatter automatically.\n"
        )

    @staticmethod
    def _build_user_message(frontmatter: dict[str, Any], body: str) -> str:
        """Build the user message containing the playbook markdown to compile.

        Includes both the frontmatter metadata (for context) and the markdown
        body (the natural language process description).
        """
        fm_summary_parts = [
            f"- **id:** {frontmatter.get('id', 'unknown')}",
            f"- **triggers:** {frontmatter.get('triggers', [])}",
            f"- **scope:** {frontmatter.get('scope', 'system')}",
        ]
        if frontmatter.get("cooldown"):
            fm_summary_parts.append(f"- **cooldown:** {frontmatter['cooldown']} seconds")

        fm_summary = "\n".join(fm_summary_parts)

        return (
            "Compile the following playbook into a JSON workflow graph.\n\n"
            "## Playbook Metadata (from frontmatter)\n\n"
            f"{fm_summary}\n\n"
            "## Playbook Content\n\n"
            f"{body.strip()}\n\n"
            "Remember: output ONLY the JSON object inside a ```json code block. "
            "Do NOT include id, version, source_hash, triggers, or scope — "
            "those are injected automatically from the frontmatter."
        )

    @staticmethod
    def _build_retry_message(errors: list[str]) -> str:
        """Build a follow-up message requesting fixes for validation errors."""
        error_list = "\n".join(f"- {e}" for e in errors)
        return (
            "The JSON you produced has validation errors:\n\n"
            f"{error_list}\n\n"
            "Please fix these errors and output the corrected JSON inside "
            "a ```json code block. Remember the rules from the system prompt."
        )

    # -- JSON extraction -----------------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Extract a JSON object from the LLM response text.

        Tries three strategies in order:
        1. Fenced ``json`` code block (```json ... ```)
        2. Any fenced code block (``` ... ```)
        3. Bare JSON object (first ``{`` to last ``}``)

        Returns ``None`` if no valid JSON object can be extracted.
        """
        # Strategy 1: fenced json code block
        match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Strategy 2: any fenced code block
        match = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Strategy 3: bare JSON — find outermost { ... }
        first_brace = text.find("{")
        last_brace = text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            try:
                return json.loads(text[first_brace : last_brace + 1])
            except json.JSONDecodeError:
                pass

        return None

    # -- merging frontmatter into compiled output ----------------------------

    @staticmethod
    def _merge_frontmatter(
        compiled: dict[str, Any],
        frontmatter: dict[str, Any],
        source_hash: str,
        version: int,
    ) -> dict[str, Any]:
        """Merge authoritative frontmatter fields into the compiled JSON.

        Frontmatter values always win — the LLM is told not to include them,
        but if it does, they are overwritten.  This ensures the ``id``,
        ``triggers``, ``scope``, etc. always match the source file's YAML.

        Also injects ``source_hash``, ``version``, and ``compiled_at`` which
        are computed by the compiler, not authored.
        """
        from datetime import datetime, timezone

        result = dict(compiled)

        # Authoritative fields from frontmatter
        result["id"] = frontmatter["id"]
        result["triggers"] = frontmatter["triggers"]
        result["scope"] = frontmatter["scope"]
        result["source_hash"] = source_hash
        result["version"] = version
        result["compiled_at"] = datetime.now(timezone.utc).isoformat()

        # Optional frontmatter fields
        if "cooldown" in frontmatter:
            result["cooldown_seconds"] = int(frontmatter["cooldown"])

        return result
