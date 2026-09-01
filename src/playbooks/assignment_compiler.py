"""Deterministic compiler for the assignment router's single LLM node."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib

import yaml

from src.playbooks.compiler import CompilationResult
from src.playbooks.models import CompiledPlaybook, LlmConfig, PlaybookNode


_CONTRACT = """

## AQ assignment contract

Return one JSON object with a `decisions` array. Include exactly one decision
for every input task. Each decision may contain only `task_id`, `input_hash`,
`intelligence_class`, `provider`, and `reason`. Use only class/provider options
supplied in the event. `provider` may be null. Keep `reason` short.

Never choose profile_id, workspace, lifecycle, pool, agent, or session. Do not
call tools or perform the task. Your only job is to make the assignment route.
"""


def _failure(*errors: str) -> CompilationResult:
    return CompilationResult(success=False, errors=list(errors))


def compile_assignment_playbook(
    markdown: str, *, existing_version: int = 0
) -> CompilationResult:
    """Compile editable routing guidance into one fixed, tool-free graph."""

    if not markdown.startswith("---"):
        return _failure("Missing YAML frontmatter")
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return _failure("Unterminated YAML frontmatter")
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        return _failure(f"Invalid YAML frontmatter: {exc}")
    body = parts[2].strip()

    errors: list[str] = []
    if frontmatter.get("kind") != "assignment-routing":
        errors.append("Assignment compiler requires 'kind: assignment-routing'")
    if frontmatter.get("role") != "assignment-routing":
        errors.append("Assignment compiler requires 'role: assignment-routing'")
    if not frontmatter.get("id"):
        errors.append("Frontmatter requires 'id'")
    if frontmatter.get("scope") not in {"system", "project"}:
        errors.append("Assignment playbook scope must be 'system' or 'project'")
    triggers = frontmatter.get("triggers")
    if not isinstance(triggers, list) or not triggers:
        errors.append("Frontmatter 'triggers' must be a non-empty list")
    if not body:
        errors.append("Assignment playbook body must contain routing guidance")
    if errors:
        return _failure(*errors)

    llm_config = frontmatter.get("llm_config")
    if llm_config is not None and not isinstance(llm_config, dict):
        return _failure("Frontmatter 'llm_config' must be an object")

    source_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]
    playbook = CompiledPlaybook(
        id=str(frontmatter["id"]),
        version=existing_version + 1,
        source_hash=source_hash,
        triggers=triggers,
        scope=str(frontmatter["scope"]),
        nodes={
            "choose": PlaybookNode(
                prompt=body + _CONTRACT,
                entry=True,
                goto="done",
            ),
            "done": PlaybookNode(terminal=True),
        },
        max_tokens=frontmatter.get("max_tokens", 1024),
        llm_config=LlmConfig.from_dict(llm_config) if llm_config else None,
        compiled_at=datetime.now(timezone.utc).isoformat(),
        enabled=bool(frontmatter.get("enabled", True)),
        kind="assignment-routing",
        role="assignment-routing",
    )
    validation_errors = playbook.validate()
    if validation_errors:
        return _failure(*validation_errors)
    return CompilationResult(
        success=True,
        playbook=playbook,
        source_hash=source_hash,
        raw_json=playbook.to_dict(),
    )
