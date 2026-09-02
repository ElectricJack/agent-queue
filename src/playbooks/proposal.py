"""Pure Markdown-to-V2 proposal assembly.

This module intentionally has no store, manager, activation, or database
imports.  A proposal is review material; callers must hand it to the later
activation package explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
    commands: dict[str, str] = {}
    profiles_out: dict[str, str] = {}
    for step in artifact.steps.values():
        command = getattr(step, "command", None)
        if command:
            contract = contracts.get(command)
            if contract is not None:
                commands[command] = contract.execution_fingerprint
        profile_id = getattr(step, "profile_id", None)
        if profile_id:
            policy = profiles.policy(profile_id)
            if policy is not None:
                profiles_out[profile_id] = policy.fingerprint()
    return artifact.model_copy(
        update={"compiled_against": CompiledAgainst(commands=commands, profiles=profiles_out)}
    )


def propose(
    source: PlaybookSource,
    body: Mapping[str, Any],
    *,
    baseline: PlaybookDefinition | None = None,
    contracts: ContractLookup,
    profiles: ProfileLookup,
    events: EventSchemaLookup,
    version: int,
) -> CompileProposal:
    """Build a review-only proposal from trusted source and untrusted body."""
    diagnostics: list[Diagnostic] = []
    clean = dict(body)
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
            inventory=source.inventory,
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
