"""Frozen adapter vocabulary for the human-ask implementation specification §3–4.

These are internal server contracts, not request models. Authentication/scope must
be resolved by CommandHandler before constructing a DecisionActor; adapters must
revalidate source authority on the caller's locked connection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

JsonObject = dict[str, Any]
MAX_CONTEXT_BYTES = 65_536
MAX_LINKS = 20
MAX_BLOCKED_WORK = 50
PREVIEW_TTL_SECONDS = 600


class AskError(ValueError):
    def __init__(self, code: str, message: str, *, fields: tuple[str, ...] = ()):
        super().__init__(message)
        self.code = code
        self.fields = fields


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class SourceIdentity:
    project_id: str
    kind: str
    key: str
    reference: JsonObject

    def __post_init__(self):
        if (
            not self.project_id
            or not 1 <= len(self.kind) <= 128
            or not 1 <= len(self.key) <= 512
            or not self.reference
        ):
            raise AskError(
                "ask.unsupported_source", "A complete canonical source identity is required"
            )


@dataclass(frozen=True)
class SourceVersion:
    decision_generation: int
    source_fingerprint: str
    feedback_version: int


@dataclass(frozen=True)
class AskChoice:
    key: str
    label: str
    effect: str
    application: Literal["immediate", "interpretation", "packet"] = "packet"


@dataclass(frozen=True)
class DecisionActor:
    """Server-authenticated principal/input; never populated from actor labels in a request."""

    principal_id: str
    provenance: JsonObject
    input_id: str | None = None

    def __post_init__(self):
        if not self.principal_id or not self.provenance:
            raise AskError("ask.not_decider", "Authenticated principal and provenance are required")


@dataclass(frozen=True)
class SourceSnapshot:
    identity: SourceIdentity
    # Includes every version affecting application, including feedback/pending state.
    fingerprint: str
    # Excludes feedback and presentation; includes revision/meaning/authority changes.
    generation_token: str
    context: JsonObject
    audience: JsonObject
    logical_owner: str
    decider: JsonObject
    choices: tuple[AskChoice, ...]
    feedback_version: int = 0
    severity: str = "medium"
    next_actor: Literal["human", "agent", "none"] = "human"
    decision_state: str = "pending"
    application_state: str = "not_started"

    @property
    def generation_fingerprint(self) -> str:
        return digest(
            {
                "source": asdict(self.identity),
                "token": self.generation_token,
                "audience": self.audience,
                "owner": self.logical_owner,
                "decider": self.decider,
                "choices": [asdict(choice) for choice in self.choices],
                "blocked_work": self.context.get("blocked_work"),
                "waiting_effect": self.context.get("waiting_effect"),
            }
        )

    def public_context(self) -> JsonObject:
        return {**self.context, "choices": [asdict(choice) for choice in self.choices]}


@dataclass(frozen=True)
class AliasReference:
    kind: str
    key: str
    source_generation: int
    source_reference: JsonObject
    provenance: JsonObject


@dataclass(frozen=True)
class Publication:
    ask: JsonObject
    created: bool
    reconciliation_case_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionIntent:
    option_key: str
    expected: SourceVersion
    overall_note: str = ""
    dispositions: tuple[JsonObject, ...] = ()
    responder_class: str | None = None

    def __post_init__(self):
        if not self.option_key or len(self.overall_note) > 16_000:
            raise AskError("ask.context_missing", "Invalid decision option or note")


@dataclass(frozen=True)
class OperationResult:
    state: Literal["succeeded", "failed", "unknown"]
    receipt: JsonObject = field(default_factory=dict)
    # Only a source reconciliation receipt can authorize retry of unknown work.
    retry_safe: bool = False


def validate_context(snapshot: SourceSnapshot) -> tuple[str, ...]:
    """Return missing/invalid fields without truncating any decision-critical content."""
    context = snapshot.context
    invalid: list[str] = []
    for name, limit in (("title", 200), ("why", 2000), ("waiting_effect", 2000)):
        value = context.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            invalid.append(name)
    for name, value in (
        ("logical_owner", snapshot.logical_owner),
        ("decider", snapshot.decider),
        ("audience", snapshot.audience),
        ("source_fingerprint", snapshot.fingerprint),
        ("generation_token", snapshot.generation_token),
    ):
        if not value:
            invalid.append(name)
    requested = context.get("requested_by")
    if not isinstance(requested, dict) or not requested.get("label") or not requested.get("origin"):
        invalid.append("requested_by")
    for name, limit in (("links", MAX_LINKS), ("blocked_work", MAX_BLOCKED_WORK)):
        items = context.get(name)
        if (
            not isinstance(items, list)
            or len(items) > limit
            or any(
                not isinstance(item, dict)
                or not item.get("label")
                or not item.get("url")
                or len(str(item["label"])) > 200
                or len(str(item["url"])) > 2048
                for item in items
            )
        ):
            invalid.append(name)
    if not context.get("links"):
        invalid.append("links")
    if context.get("blocked_work") == [] and not context.get("blocked_work_note"):
        invalid.append("blocked_work_note")
    if not 1 <= len(snapshot.choices) <= 20:
        invalid.append("choices")
    keys = set()
    for index, choice in enumerate(snapshot.choices):
        if (
            not choice.key
            or len(choice.key) > 128
            or choice.key in keys
            or not choice.label.strip()
            or len(choice.label) > 200
            or not choice.effect.strip()
            or len(choice.effect) > 2000
            or choice.application not in {"immediate", "interpretation", "packet"}
        ):
            invalid.append(f"choices[{index}]")
        keys.add(choice.key)
    if len(canonical_json(snapshot.public_context())) > MAX_CONTEXT_BYTES:
        invalid.append("context")
    return tuple(dict.fromkeys(invalid))
