"""Strict portable policy types shared by archive readers and command writers."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from src.portable_setup import PoolSettings, ProjectSettings

Scope = Literal["project", "system"]
Placement = Literal["project", "global", "skip"]
PolicyType = Literal[
    "playbook", "agent_settings", "promotion_flow", "ci_test", "routing", "template",
    "intelligence_class", "pool", "project_settings",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_mode_override="validation")


class AgentConfig(Strict):
    harness: str | None = None
    default_class: str | None = None
    needs_workspace: bool | None = None
    lifecycle: Literal["task", "pool", "named"] | None = None
    workspaces: list[str] | None = None
    permission_mode: str | None = None
    codex_full_auto: bool | None = None
    codex_service_tier: Literal["default", "fast"] | None = None
    claude_dangerously_skip_permissions: bool | None = None
    read_only: bool | None = None
    enabled: bool | None = None
    min_active: int | None = None
    max_active: int | None = None
    min_per_project: int | None = None
    max_claims_per_session: int | None = None
    mode: str | None = None
    wake_mode: str | None = None
    idle_timeout: int | None = None
    max_session_age: int | None = None
    allow_base_checkout: bool | None = None


class Capabilities(Strict):
    harness_tools: list[str] = []
    aq_commands: list[str] = []
    plugin_tools: list[str] = []


class PolicyAgentSettings(Strict):
    type: Literal["agent_settings"] = "agent_settings"
    name: str
    extends: str = ""
    template: bool = False
    config: AgentConfig = Field(default_factory=AgentConfig)
    pool: PoolSettings | None = None
    capabilities: Capabilities | None = None
    role: str = ""
    rules: str = ""
    override: bool = False
    allowed_tools: list[str] = []


class PlaybookPolicy(Strict):
    type: Literal["playbook"] = "playbook"
    source: str
    artifact: dict[str, Any]
    dependencies: list[str] = []


class PromotionPolicy(Strict):
    type: Literal["promotion_flow"] = "promotion_flow"
    flow: list[dict[str, Any]]


class PathAreas(Strict):
    paths: list[str]
    areas: list[str]


class ScopedConftest(Strict):
    path: str
    subtree: str


class SourceScan(Strict):
    modules: list[str]
    triggers: list[str]


class SelectionRules(Strict):
    version: Literal[1] = 1
    global_invalidators: list[str] = []
    scoped_conftests: list[ScopedConftest] = []
    non_behavioral: list[str] = []
    ownership: list[PathAreas] = []
    source_scanning: list[SourceScan] = []
    critical: list[str] = []


class Area(Strict):
    id: str
    description: str
    match: list[str]


class SelectionAreas(Strict):
    version: Literal[1] = 1
    areas: list[Area]


class Omission(Strict):
    choice: Literal["unaffected"] = "unaffected"
    min_probability: float = Field(ge=0, le=1)
    min_confidence: float = Field(ge=0, le=1)


class SelectionPolicy(Strict):
    version: Literal[1] = 1
    question_schema_version: int
    model: str
    omission: Omission


class RequiredChecks(Strict):
    version: str
    names: list[str]


class CIPolicy(Strict):
    required_checks: RequiredChecks | None = None
    check_sets: dict[str, list[str]] = {}
    promotion_attestation_names: list[str] = []


class CITestPolicy(Strict):
    type: Literal["ci_test"] = "ci_test"
    ci: CIPolicy | None = None
    selection_rules: SelectionRules | None = None
    selection_areas: SelectionAreas | None = None
    selection_policy: SelectionPolicy | None = None


class RoutingPreferences(Strict):
    type: Literal["routing"] = "routing"
    assignment_playbook_id: str
    # The runtime's strict RoutingPolicy validates this mapping, including
    # nested lanes/risk rules; there is no provider/account configuration.
    policy: dict[str, Any] | None = None


class TemplatePolicy(Strict):
    type: Literal["template"] = "template"
    kind: Literal["spec", "plan", "formula"]
    content: str


class IntelligenceClassPolicy(Strict):
    type: Literal["intelligence_class"] = "intelligence_class"
    source: str


class PoolPolicy(Strict):
    type: Literal["pool"] = "pool"
    settings: PoolSettings


class ProjectPolicy(Strict):
    type: Literal["project_settings"] = "project_settings"
    settings: ProjectSettings


Payload = Annotated[
    PlaybookPolicy
    | PolicyAgentSettings
    | PromotionPolicy
    | CITestPolicy
    | RoutingPreferences
    | TemplatePolicy
    | IntelligenceClassPolicy
    | PoolPolicy
    | ProjectPolicy,
    Field(discriminator="type"),
]
PAYLOAD = TypeAdapter(Payload)


def encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(encoded(value)).hexdigest()


class Placeholder(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str


class Item(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,160}$")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
    type: PolicyType
    original_scope: Scope
    checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    payload: Payload

    @model_validator(mode="before")
    @classmethod
    def normalize_verified_payload(cls, value):
        # Verify the bytes' logical payload before filling new optional fields.
        # Older v1 archives did not serialize fields added by newer adapters.
        if isinstance(value, dict) and isinstance(value.get("payload"), dict):
            payload = PAYLOAD.validate_python(value["payload"])
            if checksum(value["payload"]) != value.get("checksum"):
                raise ValueError(f"item {value.get('id')}: payload checksum mismatch")
            value = {**value, "payload": payload,
                     "checksum": checksum(payload.model_dump(mode="json"))}
        return value

    @model_validator(mode="after")
    def consistent(self) -> Item:
        if (
            self.type != self.payload.type
            or checksum(self.payload.model_dump(mode="json")) != self.checksum
        ):
            raise ValueError(f"item {self.id}: payload type or checksum mismatch")
        return self


class Bundle(Strict):
    format: Literal["aq.policy.v1"] = "aq.policy.v1"
    name: str = Field(min_length=1, max_length=200)
    version: Literal[1] = 1
    source_aq_version: str
    items: list[Item] = Field(max_length=1000)
    placeholders: list[Placeholder] = []

    @model_validator(mode="after")
    def unique(self) -> Bundle:
        ids = [item.id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate policy item ids")
        names = [p.name for p in self.placeholders]
        if len(set(names)) != len(names):
            raise ValueError("duplicate placeholders")
        return self


class Selection(Strict):
    scope: Placement = "project"
    overwrite: bool = False
    expected_checksum: str | None = None


class DiffItem(Strict):
    id: str
    name: str
    type: PolicyType
    original_scope: Scope
    scope: Placement
    status: Literal["new", "identical", "will overwrite"]
    selected: bool
    requires_scope_choice: bool
    current_checksum: str | None = None
    diff: str = ""
    destinations: list[str] = []
    state: Literal["copy", "pending review", "pending configuration", "system template"] = "copy"


class ExportRequest(Strict):
    project_id: str
    name: str | None = None
    path: str | None = None
    expected_checksum: str | None = None


class ImportRequest(Strict):
    project_id: str
    builtin: Literal["default"] | None = None
    path: str | None = None
    bundle: Bundle | None = None
    archive: str | None = Field(default=None, max_length=45_000_000)
    values: dict[str, str] = {}
    selections: dict[str, Selection] = {}
    only: list[str] = []
    skip: list[str] = []
    no_overwrite: bool = False

    @model_validator(mode="after")
    def one_source(self) -> ImportRequest:
        sources = (self.path, self.bundle, self.archive, self.builtin)
        if sum(source is not None for source in sources) != 1:
            raise ValueError("provide exactly one of path, bundle, archive or builtin")
        return self
