"""Render and read frozen Development settings beside a reviewed decision table.

Rendering is review preparation only. Runtime loads the source and artifact by
the subject's immutable pin; it never reads the current project's settings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.integration.development import DevelopmentPolicy
from src.playbooks.definition import PlaybookDefinition, source_digest
from src.integration.runtime_contracts import PolicyArtifactPin
from src.playbooks.integration_policy import CompiledIntegrationPolicy, policy_from_markdown

TEMPLATE = Path(__file__).with_name("development_policy_template.md")


def render_development_policy(project_id: str, settings: DevelopmentPolicy) -> str:
    """Produce a project-owned source for the existing review/import workflow."""
    if not project_id:
        raise ValueError("a development policy needs its project")
    settings = settings.model_copy(deep=True).checked()
    template = TEMPLATE.read_text()
    policy = policy_from_markdown(template).model_dump(mode="json", exclude_none=True)
    root = policy["tables"]["root_batch"]
    cadence = root["actions"]["cadence"]
    cadence["inputs"]["seconds"]["value"] = settings.interval_seconds
    cadence["outcomes"]["waiting"]["seconds"] = settings.interval_seconds
    policy["max_wait_seconds"] = max(3600, settings.interval_seconds)
    root["actions"]["seal"]["inputs"]["admission"]["value"]["max_members"] = (
        settings.max_batch_size
    )
    root["actions"]["build"]["inputs"]["regenerate_generated"]["value"] = bool(
        settings.regenerate
    )
    if settings.validation == "none":
        root["actions"]["build"]["outcomes"]["merged"]["phase"] = "publishing"
    if settings.validation == "advisory":
        root["actions"]["observe-ci"]["outcomes"]["red"]["phase"] = "publishing"
    metadata = {
        "id": f"{project_id}-development",
        "scope": f"project:{project_id}",
        "enabled": False,
        "triggers": ["integration.subject_due"],
        "development": settings.model_dump(mode="json"),
    }
    prose = template.split("---", 2)[2].split("```integration-policy", 1)[0]
    rendered = (
        "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + prose
        + "```integration-policy\n" + json.dumps(policy, indent=2) + "\n```\n"
    )
    policy_from_markdown(rendered)  # Apply the shared compiler's complete checks.
    return rendered


@dataclass(frozen=True)
class PinnedDevelopmentPolicy:
    """The retained source makes validation commands/budgets part of the pin."""

    definition: PlaybookDefinition
    source: str

    def __post_init__(self):
        if self.definition.source_hash != source_digest(self.source):
            raise ValueError("development source does not match the reviewed artifact")
        parts = self.source.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("development source needs frontmatter")
        metadata = yaml.safe_load(parts[1])
        if (
            metadata["id"] != self.definition.id
            or self.definition.scope.type != "project"
            or metadata["scope"] != f"project:{self.definition.scope.project_id}"
        ):
            raise ValueError("development source belongs to another project/artifact")
        if policy_from_markdown(self.source) != self.definition.integration_policy:
            raise ValueError("development table differs from its reviewed source")
        DevelopmentPolicy.model_validate(metadata["development"]).checked()

    @property
    def settings(self) -> DevelopmentPolicy:
        metadata = yaml.safe_load(self.source.split("---", 2)[1])
        return DevelopmentPolicy.model_validate(metadata["development"]).checked()

    @property
    def compiled(self) -> CompiledIntegrationPolicy:
        return CompiledIntegrationPolicy(self.definition)


def project_development_pin(project) -> PolicyArtifactPin | None:
    """The project's reviewed, imported Development artifact pin.

    Project settings cannot change an in-flight subject: the pin is what makes
    a subject load its own reviewed source. A project without one is never
    seeded.
    """
    policy = project.get("hierarchical_integration_policy") or {}
    boundary = policy.get("development") if isinstance(policy, dict) else None
    artifact = (boundary or {}).get("route", {}).get("artifact") if boundary else None
    if not artifact or not artifact.get("artifact_sha256"):
        return None
    return PolicyArtifactPin(
        playbook_id=artifact["playbook_id"], artifact_sha256=artifact["artifact_sha256"]
    )


def load_pinned_development_policy(config, artifact_sha256, definition):
    """Load retained reviewed source, with a hash-checked bridge for old imports."""
    from src.playbooks.artifact_store import ArtifactStore

    store = ArtifactStore(config.compiled_root)
    try:
        source = store.load_source(artifact_sha256)
    except FileNotFoundError:
        # The compiled schema never contained Markdown. Older installs can
        # bridge from the vault only while its source matches the frozen pin.
        vault = Path(config.vault_root).expanduser().resolve()
        path = (vault / "projects" / definition.scope.project_id / "playbooks"
                / f"{definition.id}.md").resolve()
        path.relative_to(vault)
        source = path.read_text(encoding="utf-8")
    return PinnedDevelopmentPolicy(definition, source)
