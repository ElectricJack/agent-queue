"""Pure Markdown-to-V2 proposal assembly.

This module intentionally has no store, manager, activation, or database
imports.  A proposal is review material; callers must hand it to the later
activation package explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any, Final, Mapping

from pydantic import ValidationError

from src.playbooks.authoring import PlaybookSource
from src.playbooks.definition import (
    COMPILER_BUILD,
    CompiledAgainst,
    PlaybookDefinition,
    Rule,
    Step,
    artifact_sha256,
    contract_fingerprint,
    scope_from_v1,
    source_digest,
    step_profile_ids,
    truncate_excerpt,
)
from src.playbooks.semantic_diff import DefinitionDiff, diff_definitions
from src.playbooks.validation import (
    ContractLookup,
    Diagnostic,
    EventSchemaLookup,
    ProfileLookup,
    validate_definition,
)
from src.playbooks.expressions import V2Base

AUTHORITATIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "id",
        "version",
        "scope",
        "purpose",
        "source_hash",
        "compiled_at",
        "compiler_build",
        "compiled_against",
        "enabled",
        "triggers",
        "cooldown_seconds",
        "max_tokens",
        "llm_config",
        "transition_llm_config",
        "profile_id",
        "kind",
        "role",
    }
)


class SemanticBody(V2Base):
    rules: list[Rule]
    steps: dict[str, Step]


class DuplicateSemanticKey(ValueError):
    """Raised before model validation when a proposal repeats a JSON key."""


def load_semantic_body_json(text: str) -> Mapping[str, Any]:
    """Parse an untrusted semantic body without last-key-wins ambiguity."""

    def reject(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateSemanticKey(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=reject)
    if not isinstance(value, Mapping):
        raise ValueError("semantic body must be a JSON object")
    return value


@dataclass(frozen=True)
class CompileProposal:
    artifact: PlaybookDefinition | None
    diagnostics: list[Diagnostic]
    questions: list[Diagnostic]
    source_digest: str
    contract_fingerprint: str | None
    compiler_build: str
    semantic_diff: DefinitionDiff | None
    artifact_sha256: str | None

    @property
    def activatable(self) -> bool:
        return self.artifact is not None and not any(
            d.severity in {"error", "question"} for d in self.diagnostics
        )


def _diagnostic_from_error(error: Mapping[str, Any], source: PlaybookSource) -> Diagnostic:
    loc = tuple(error.get("loc", ()))
    # Pydantic's union locations are not stable enough to make a synthetic
    # source assertion.  A source-level error is honest and still actionable.
    return Diagnostic(
        "error",
        "ambiguous_prose",
        str(error.get("msg", "invalid semantic body")),
        field="/" + "/".join(map(str, loc)) or None,
        source=source.inventory.refs(source.frontmatter.get("id", ""))[0]
        if source.inventory.refs(source.frontmatter.get("id", ""))
        else None,
    )


def _trusted_purpose(source: PlaybookSource) -> str:
    return (
        "assignment_routing"
        if source.frontmatter.get("kind") == "assignment-routing"
        else "routine"
    )


def _snapshots(
    artifact: PlaybookDefinition, contracts: ContractLookup, profiles: ProfileLookup
) -> PlaybookDefinition:
    """Recompute ``compiled_against`` from the live registries (§7.1).

    ``profiles`` covers both positions :func:`step_profile_ids` knows about —
    a step's own profile and a *delegated* one handed to a command as a literal
    ``profile_id`` argument.  The delegated half is what the shipped
    ``default-pipeline`` uses exclusively: it has no AI step at all, and its
    dependency on ``reviewer`` / ``final-reviewer`` / ``spec-ingest`` exists
    only as ``ensure_task`` arguments.  Recording only the own-profile half
    left that artifact with an empty map, so a capability change could never
    stale it.

    An unresolvable profile is left out rather than recorded as empty: the map
    is a *fingerprint of what exists*, and a missing entry is how
    ``evaluate_health`` reads "not registered here".
    """
    commands: dict[str, str] = {}
    profiles_out: dict[str, str] = {}
    for step in artifact.steps.values():
        command = getattr(step, "command", None)
        command_names = [command] if command else []
        tool_use = getattr(step, "tool_use", None)
        if tool_use is not None:
            command_names.extend(tool_use.aq_commands)
            command_names.extend(tool_use.plugin_tools)
        for command_name in command_names:
            contract = contracts.get(command_name)
            if contract is not None:
                commands[command_name] = contract.execution_fingerprint
        for profile_id in step_profile_ids(step):
            policy = profiles.policy(profile_id)
            if policy is not None:
                profiles_out[profile_id] = policy.fingerprint()
    return artifact.model_copy(
        update={"compiled_against": CompiledAgainst(commands=commands, profiles=profiles_out)}
    )


def _sanitize_source_refs(value: Any, source: PlaybookSource) -> tuple[Any, list[Diagnostic]]:
    """Bound compiler-supplied source references before Pydantic loads them.

    Source references are presentation data, but they are still untrusted
    compiler output.  Keep an out-of-range reference visible as an error and
    truncate an overlong excerpt deterministically so the rest of the proposal
    remains reviewable instead of collapsing into an unrelated model error.
    """
    diagnostics: list[Diagnostic] = []
    line_count = max(1, len(source.raw.splitlines()))

    def walk(item: Any) -> Any:
        if isinstance(item, list):
            return [walk(child) for child in item]
        if not isinstance(item, Mapping):
            return item
        clean = {key: walk(child) for key, child in item.items()}
        if {"path", "start_line", "end_line"} <= clean.keys():
            start = clean.get("start_line")
            end = clean.get("end_line")
            if (
                isinstance(start, int)
                and isinstance(end, int)
                and (start < 1 or end < start or end > line_count)
            ):
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "source_ref_out_of_range",
                        f"source reference lines {start}-{end} are outside 1-{line_count}",
                        source=None,
                    )
                )
            excerpt = clean.get("excerpt")
            if isinstance(excerpt, str):
                bounded, truncated = truncate_excerpt(excerpt)
                clean["excerpt"] = bounded
                if truncated:
                    diagnostics.append(
                        Diagnostic(
                            "info",
                            "excerpt_truncated",
                            "source excerpt was truncated to 400 characters",
                            source=None,
                        )
                    )
        return clean

    return walk(value), diagnostics


def propose(
    source: PlaybookSource,
    body: Mapping[str, Any],
    *,
    baseline: PlaybookDefinition | None = None,
    contracts: ContractLookup,
    profiles: ProfileLookup,
    events: EventSchemaLookup,
    version: int,
    enforce_inventory: bool = True,
) -> CompileProposal:
    """Build a review-only proposal from trusted source and untrusted body."""
    diagnostics: list[Diagnostic] = []
    clean, source_diagnostics = _sanitize_source_refs(dict(body), source)
    diagnostics.extend(source_diagnostics)
    for key in sorted(AUTHORITATIVE_FIELDS & clean.keys()):
        clean.pop(key)
        diagnostics.append(
            Diagnostic(
                "warning",
                "authority_field_ignored",
                f"compiler-supplied {key!r} was discarded; it is server-owned",
                field=f"/{key}",
            )
        )
    try:
        semantic = SemanticBody.model_validate(clean)
    except ValidationError as exc:
        diagnostics.extend(_diagnostic_from_error(error, source) for error in exc.errors())
        digest = source_digest(source.raw)
        return CompileProposal(
            None,
            diagnostics,
            [d for d in diagnostics if d.severity == "question"],
            digest,
            None,
            COMPILER_BUILD,
            None,
            None,
        )
    try:
        artifact = PlaybookDefinition(
            id=source.frontmatter["id"],
            version=version,
            scope=scope_from_v1(source.frontmatter["scope"]),
            purpose=_trusted_purpose(source),
            source_hash=source_digest(source.raw),
            compiled_at=datetime.now(UTC),
            compiler_build=COMPILER_BUILD,
            rules=semantic.rules,
            steps=semantic.steps,
        )
    except (KeyError, ValueError, ValidationError) as exc:
        diagnostics.append(Diagnostic("error", "ambiguous_prose", str(exc)))
        digest = source_digest(source.raw)
        return CompileProposal(None, diagnostics, [], digest, None, COMPILER_BUILD, None, None)
    artifact = _snapshots(artifact, contracts, profiles)
    diagnostics.extend(
        validate_definition(
            artifact,
            inventory=source.inventory if enforce_inventory else None,
            contracts=contracts,
            profiles=profiles,
            events=events,
        )
    )
    diff = diff_definitions(baseline, artifact) if baseline is not None else None
    return CompileProposal(
        artifact,
        diagnostics,
        [d for d in diagnostics if d.severity == "question"],
        source_digest(source.raw),
        contract_fingerprint(artifact),
        COMPILER_BUILD,
        diff,
        artifact_sha256(artifact),
    )
