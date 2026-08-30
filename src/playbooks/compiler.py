"""Playbook compiler — deterministic path only.

Historical: this module also housed an LLM-driven compiler that turned
natural-language playbook markdown into a compiled JSON graph.  Phase 6
(compiler-as-agent) removed that path — non-pipeline compilations are
now enqueued as tasks against the ``playbook-compiler`` agent-type
profile, which iterates against ``playbook_validate`` / ``playbook_install``
until the artifact is accepted.

What remains here:

- :class:`CompilationResult` — the shared result type used by both the
  pipeline compiler (see ``src/playbooks/pipeline_compiler.py``) and the
  ``playbook_validate`` command.
- :func:`compile_playbook` — a synchronous dispatch helper that accepts a
  markdown file and routes ``kind: pipeline`` files to the deterministic
  pipeline compiler.  Non-pipeline files return a failure result telling
  callers to route the compile through the agent instead.
- :class:`PlaybookCompiler` — a thin holder for the surviving static
  helpers (``_parse_frontmatter``, ``_validate_frontmatter``,
  ``_compute_source_hash``, ``_normalize_content``, ``_merge_frontmatter``,
  ``_extract_json``) plus a :meth:`compile_pipeline` method that mirrors
  :func:`compile_playbook` for the pipeline case.

There is no async ``compile()`` method, no chat-provider import, and no
provider parameter — the framework does not call any LLM from this
module.  See ``docs/specs/design/playbooks.md`` §4.6.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from src.playbooks.models import CompiledPlaybook

logger = logging.getLogger(__name__)

# Fallback max_tokens kept as a module constant for callers (e.g. the
# manager) that plumb a token budget through unchanged config.  The value
# is inert here — no LLM call happens in this module.
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
        raw_json: The raw JSON dict extracted from the compiler output,
            before dataclass conversion.  Useful for debugging.
        retries_used: Kept for backward compatibility with pre-Phase-6
            callers (always 0 for the deterministic path).
        skipped: ``True`` if compilation was skipped because the source
            markdown has not changed since the last successful compilation.
        structured_errors: ``[{node, field, message}, ...]`` records
            matching the Phase 6 ``playbook_validate`` contract.
    """

    success: bool
    playbook: CompiledPlaybook | None = None
    errors: list[str] = field(default_factory=list)
    source_hash: str = ""
    raw_json: dict[str, Any] | None = None
    retries_used: int = 0
    skipped: bool = False
    structured_errors: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Dispatch helper
# ---------------------------------------------------------------------------


def compile_playbook(markdown: str, *, existing_version: int = 0) -> "CompilationResult":
    """Compile a playbook markdown file, dispatching on ``kind`` frontmatter.

    - ``kind: pipeline`` → :func:`src.playbooks.pipeline_compiler.compile_pipeline`
      (deterministic, no LLM).
    - anything else → failure result directing the caller to enqueue a
      compile task against the ``playbook-compiler`` profile.
    """
    kind = ""
    if markdown.startswith("---"):
        parts = markdown.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
                kind = fm.get("kind", "")
            except yaml.YAMLError:
                pass

    if kind == "pipeline":
        from src.playbooks.pipeline_compiler import compile_pipeline as _cp

        return _cp(markdown, existing_version=existing_version)

    return CompilationResult(
        success=False,
        errors=[
            f"compile_playbook: kind '{kind}' is compiled by the "
            "playbook-compiler agent (Phase 6). The framework no longer "
            "runs an in-process LLM for ordinary playbooks — enqueue a "
            "task with dedup_key='playbook-compile:<id>' instead."
        ],
    )


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


class PlaybookCompiler:
    """Deterministic-only compiler shell.

    Post-Phase-6 this class is a thin holder for the shared static helpers
    (``_parse_frontmatter``, ``_compute_source_hash``, …) that other
    modules still import, plus a :meth:`compile_pipeline` method that runs
    the deterministic pipeline path.

    No chat provider is accepted, no LLM call is made.
    """

    def __init__(self, *, config: Any = None) -> None:
        # ``config`` is retained for ``aq://`` URI rewriting inside future
        # deterministic transforms; the pipeline path does not currently
        # consume it, but call sites already plumb it.
        self._config = config

    # -- public API ----------------------------------------------------------

    def compile_pipeline(
        self, markdown: str, *, existing_version: int = 0
    ) -> CompilationResult:
        """Deterministic pipeline compile — thin instance-method wrapper."""
        from src.playbooks.pipeline_compiler import compile_pipeline as _cp

        return _cp(markdown, existing_version=existing_version)

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
        else:
            for i, t in enumerate(triggers):
                if isinstance(t, str):
                    if not t:
                        errors.append(
                            f"Frontmatter 'triggers[{i}]': string trigger must be non-empty"
                        )
                elif isinstance(t, dict):
                    event_type = t.get("type") or t.get("event_type")
                    if not event_type or not isinstance(event_type, str):
                        errors.append(
                            f"Frontmatter 'triggers[{i}]': structured trigger must have "
                            "a non-empty 'type' (or 'event_type') string"
                        )
                    if "filter" in t and not isinstance(t.get("filter"), dict):
                        errors.append(f"Frontmatter 'triggers[{i}]': 'filter' must be a dict")
                else:
                    errors.append(
                        f"Frontmatter 'triggers[{i}]': must be a string or dict, "
                        f"got {type(t).__name__}"
                    )

        if not frontmatter.get("scope"):
            errors.append("Frontmatter missing required field: 'scope'")
        else:
            scope = frontmatter["scope"]
            if scope not in ("system", "project") and not scope.startswith("agent-type:"):
                errors.append(
                    f"Frontmatter 'scope' must be 'system', 'project', or "
                    f"'agent-type:{{type}}', got: '{scope}'"
                )

        if "enabled" in frontmatter:
            enabled = frontmatter["enabled"]
            if not isinstance(enabled, bool):
                errors.append("Frontmatter 'enabled' must be a boolean")

        if "profile_id" in frontmatter:
            pid = frontmatter["profile_id"]
            if pid is not None and not isinstance(pid, str):
                errors.append(
                    f"Frontmatter 'profile_id' must be a string, got {type(pid).__name__}"
                )

        return errors

    # -- hashing -------------------------------------------------------------

    @staticmethod
    def _normalize_content(content: str) -> str:
        """Normalize playbook markdown for stable hashing.

        Strips cosmetic differences that don't affect the compiled output:
        YAML frontmatter comments (removed by parse + re-serialize with
        sorted keys); HTML/Markdown comments (``<!-- ... -->``); trailing
        whitespace; runs of blank lines; leading/trailing blank lines.
        """
        frontmatter, body = PlaybookCompiler._parse_frontmatter(content)

        if frontmatter:
            fm_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True).strip()
        else:
            fm_str = ""

        body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)

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
        """Compute a stable SHA-256 hash (16 hex chars) of normalized markdown."""
        normalized = PlaybookCompiler._normalize_content(content)
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    # -- JSON extraction -----------------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Extract a JSON object from a body of text.

        Tries three strategies in order:

        1. Fenced ``json`` code block (```json ... ```)
        2. Any fenced code block (``` ... ```)
        3. Bare JSON object (first ``{`` to last ``}``)

        Returns ``None`` if no valid JSON object can be extracted.  Kept
        as a static helper for callers (e.g. dashboard save flow) that
        need to peel JSON out of markdown fragments.
        """
        match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        match = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

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
        """Merge authoritative frontmatter fields into a compiled JSON dict.

        Frontmatter values always win — the ``id``, ``triggers``, ``scope``,
        etc. always match the source file's YAML.  Also injects
        ``source_hash``, ``version``, and ``compiled_at`` which are
        computed by the compiler.

        Retained here so the ``playbook-compiler`` agent (or any external
        deterministic tool) can reuse the exact merge behaviour the
        Phase 5 compiler used to apply.  Body-rewrite of ``aq://`` URIs
        is preserved via :func:`~src.aq_uri.rewrite_aq_uris`.
        """
        from datetime import datetime, timezone

        result = dict(compiled)

        result["id"] = frontmatter["id"]
        raw_triggers = frontmatter["triggers"]
        normalized_triggers: list[str | dict] = []
        for t in raw_triggers:
            if isinstance(t, str):
                normalized_triggers.append(t)
            elif isinstance(t, dict):
                event_type = t.get("type") or t.get("event_type", "")
                trigger_dict: dict = {"event_type": event_type}
                if "filter" in t:
                    trigger_dict["filter"] = t["filter"]
                normalized_triggers.append(trigger_dict)
            else:
                normalized_triggers.append(t)
        result["triggers"] = normalized_triggers
        result["scope"] = frontmatter["scope"]
        result["source_hash"] = source_hash
        result["version"] = version
        result["compiled_at"] = datetime.now(timezone.utc).isoformat()

        if "cooldown" in frontmatter:
            result["cooldown_seconds"] = int(frontmatter["cooldown"])

        if "llm_config" in frontmatter and isinstance(frontmatter["llm_config"], dict):
            result["llm_config"] = frontmatter["llm_config"]
        if "transition_llm_config" in frontmatter and isinstance(
            frontmatter["transition_llm_config"], dict
        ):
            result["transition_llm_config"] = frontmatter["transition_llm_config"]
        for key in ("llm_config", "transition_llm_config"):
            cfg = result.get(key)
            if isinstance(cfg, dict):
                dropped = sorted(
                    set(cfg) - {"provider", "model", "intelligence_class", "max_tokens"}
                )
                if dropped:
                    logger.warning(
                        "playbook %s: %s keys %s are ignored",
                        result.get("id"),
                        key,
                        dropped,
                    )

        if "max_tokens" in frontmatter:
            result["max_tokens"] = int(frontmatter["max_tokens"])

        result.pop("profile_id", None)
        if "profile_id" in frontmatter and frontmatter["profile_id"]:
            result["profile_id"] = str(frontmatter["profile_id"]).strip()

        result.pop("enabled", None)
        if "enabled" in frontmatter:
            result["enabled"] = bool(frontmatter["enabled"])

        return result


__all__ = [
    "CompilationResult",
    "DEFAULT_MAX_TOKENS",
    "PlaybookCompiler",
    "compile_playbook",
]
