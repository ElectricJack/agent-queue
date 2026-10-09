"""Explicit readers and writers for the six portable policy families.

Filesystem work runs on a thread from the command adapter. The service never
reads a whole vault tree or serializes a project/profile/config object.
"""

from __future__ import annotations

import difflib
import json
import re
from importlib.metadata import version
from pathlib import Path
from typing import Any

import yaml

from src.policy_profiles.models import (
    PAYLOAD,
    AgentConfig,
    PolicyAgentSettings,
    Bundle,
    Capabilities,
    CIPolicy,
    CITestPolicy,
    DiffItem,
    Item,
    Placeholder,
    PlaybookPolicy,
    PromotionPolicy,
    RoutingPreferences,
    Selection,
    SelectionAreas,
    SelectionPolicy,
    SelectionRules,
    TemplatePolicy,
    checksum,
    encoded,
)

PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
TEST_FILES = {
    "selection_rules": ("tests/selection_rules.yaml", SelectionRules),
    "selection_areas": ("tests/selection_areas.yaml", SelectionAreas),
    "selection_policy": ("tests/selection_policy.yaml", SelectionPolicy),
}


def contained(root: Path, relative: str) -> Path:
    target = root / relative
    if target.resolve() != root.resolve() / relative or target.is_symlink():
        raise ValueError(f"policy path contains a symlink: {relative}")
    return target


def transform(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for before, after in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
            if before:
                value = re.sub(
                    r"(?<![\w-])" + re.escape(before) + r"(?![\w-])", lambda _match: after, value
                )
        return value
    if isinstance(value, list):
        return [transform(entry, replacements) for entry in value]
    if isinstance(value, dict):
        return {key: transform(entry, replacements) for key, entry in value.items()}
    return value


def item(name: str, scope: str, payload) -> Item:
    data = payload.model_dump(mode="json")
    return Item(
        id=f"{payload.type}:{scope}:{name}",
        name=name,
        type=payload.type,
        original_scope=scope,
        payload=payload,
        checksum=checksum(data),
    )


def _projection(model, document: dict):
    """Known scalar/structured settings only; extras never leave the install."""
    return model.model_validate(
        {key: document[key] for key in model.model_fields if key in document}
    )


def read_profile(path: Path, *, override: bool = False) -> PolicyAgentSettings:
    from src.profiles.parser import parse_profile

    raw = path.read_text(encoding="utf-8")
    if override:
        return PolicyAgentSettings(name=path.stem, rules=raw, override=True)
    parsed = parse_profile(raw)
    config = _projection(AgentConfig, parsed.config)
    return PolicyAgentSettings(
        name=parsed.frontmatter.name or path.parent.name,
        extends=parsed.frontmatter.extends,
        template=parsed.frontmatter.template,
        config=config,
        capabilities=Capabilities.model_validate(parsed.capabilities)
        if parsed.capabilities is not None
        else None,
        role=parsed.role,
        rules=parsed.rules,
        allowed_tools=list(parsed.tools.get("allowed", parsed.tools.get("allowed_tools", []))),
    )


def render_profile(name: str, payload: PolicyAgentSettings, existing: str = "") -> str:
    if payload.override:
        return payload.rules
    from src.profiles.parser import parse_profile

    previous = parse_profile(existing)
    header = {**previous.frontmatter.extra, "id": name, "name": payload.name}
    if previous.frontmatter.tags:
        header["tags"] = previous.frontmatter.tags
    if payload.extends:
        header["extends"] = payload.extends
    if payload.template:
        header["template"] = True
    text = "---\n" + yaml.safe_dump(header, sort_keys=False) + "---\n"
    local_config = {
        key: value for key, value in previous.config.items() if key not in AgentConfig.model_fields
    }
    for heading, value in (
        ("Config", {**local_config, **payload.config.model_dump(exclude_none=True)}),
        (
            "Capabilities",
            payload.capabilities.model_dump() if payload.capabilities is not None else None,
        ),
        (
            "Tools",
            {"allowed": payload.allowed_tools}
            if payload.capabilities is None and payload.allowed_tools
            else None,
        ),
    ):
        if value is not None:
            text += f"\n## {heading}\n```json\n{encoded(value).decode()}```\n"
    for heading, value in (("Role", payload.role), ("Rules", payload.rules)):
        if value:
            text += f"\n## {heading}\n{value}\n"
    for key, section in previous.sections.items():
        if key not in {"config", "capabilities", "tools", "role", "rules"}:
            text += f"\n## {section.heading}\n{section.raw}\n"
    return text


def validate_payload(payload) -> None:
    """Type-specific semantic validation applies equally to reads and writes."""
    if isinstance(payload, PolicyAgentSettings) and not payload.override:
        from src.profiles.parser import parse_profile

        parsed = parse_profile(render_profile("portable-profile", payload))
        if not parsed.is_valid:
            raise ValueError("invalid profile policy: " + "; ".join(parsed.errors))
    elif isinstance(payload, PlaybookPolicy):
        from src.playbooks.definition import load_definition_json, source_digest

        definition = load_definition_json(json.dumps(payload.artifact))
        if definition.source_hash != source_digest(payload.source):
            raise ValueError("playbook source does not match its compiled artifact")
    elif isinstance(payload, PromotionPolicy):
        from jsonschema import Draft202012Validator
        from src.integration.promotion_steps import FlowSchema

        errors = list(Draft202012Validator(FlowSchema.schema()).iter_errors(payload.flow))
        if errors:
            raise ValueError(f"invalid promotion draft: {errors[0].message}")
    elif isinstance(payload, RoutingPreferences) and payload.policy is not None:
        from src.routing.policy import parse_policy

        parse_policy(yaml.safe_dump(payload.policy))
    elif isinstance(payload, TemplatePolicy) and payload.kind == "formula":
        from src.profiles.parser import parse_frontmatter
        from src.task_graph.formulas import parse_formula

        metadata, _ = parse_frontmatter(payload.content)
        parse_formula(payload.content, rel_path=f"system/formulas/{metadata.name}.md")


class PolicyFiles:
    def __init__(self, vault: Path, project, workspace: str | None):
        self.vault = vault.resolve()
        self.project = project
        self.project_root = contained(self.vault, f"projects/{project.id}")
        self.workspace = Path(workspace).resolve() if workspace else None

    def root(self, scope: str) -> Path:
        return self.project_root if scope == "project" else contained(self.vault, "system")

    def _draft(self, name: str, policy_type: str, scope: str) -> Path:
        return contained(self.root(scope), f"policy/{policy_type}/{name}.json")

    def read_local(self) -> list[Item]:
        result = []
        # These six directories are typed settings storage, never an open-ended
        # vault walk. A file of an unrecognised type cannot enter the bundle.
        for scope in ("system", "project"):
            for policy_type in (
                "agent_settings",
                "promotion_flow",
                "ci_test",
                "routing",
                "template",
            ):
                directory = contained(self.root(scope), f"policy/{policy_type}")
                for path in sorted(directory.glob("*.json")):
                    contained(directory, path.name)
                    payload = PAYLOAD.validate_json(path.read_text())
                    if payload.type != policy_type:
                        raise ValueError("stored policy has the wrong type")
                    result.append(item(path.stem, scope, payload))
        profiles = contained(self.project_root, "agent-types")
        for path in sorted(profiles.glob("*/profile.md")):
            contained(profiles, path.relative_to(profiles).as_posix())
            result.append(item(path.parent.name, "project", read_profile(path)))
        overrides = contained(self.project_root, "overrides")
        for path in sorted(overrides.glob("*.md")):
            contained(overrides, path.name)
            result.append(
                item(path.stem + ".override", "project", read_profile(path, override=True))
            )
        for scope in ("project", "system"):
            for kind in ("spec", "plan"):
                directory = contained(self.root(scope), f"templates/{kind}")
                for path in sorted(directory.glob("*.md")):
                    contained(directory, path.name)
                    result.append(
                        item(
                            f"{kind}.{path.stem}",
                            scope,
                            TemplatePolicy(kind=kind, content=path.read_text()),
                        )
                    )
        for scope in ("project", "system"):
            directory = contained(self.root(scope), "formulas")
            for path in sorted(directory.glob("*.md")):
                contained(directory, path.name)
                result.append(
                    item(
                        f"formula.{path.stem}",
                        scope,
                        TemplatePolicy(kind="formula", content=path.read_text()),
                    )
                )
        if self.workspace:
            settings = {}
            for key, (relative, model) in TEST_FILES.items():
                path = contained(self.workspace, relative)
                if path.is_file():
                    settings[key] = _projection(model, yaml.safe_load(path.read_text()))
            trust = contained(self.workspace, ".github/agent-queue-integration.json")
            copied_checks = contained(self.project_root, "policy/ci-checks.json")
            if copied_checks.is_file():
                settings["ci"] = CIPolicy.model_validate_json(copied_checks.read_text())
            if trust.is_file():
                settings["ci"] = _projection(CIPolicy, json.loads(trust.read_text()))
            if settings:
                result.append(item("tests", "project", CITestPolicy(**settings)))
        return result

    def defaults(self) -> dict[str, str]:
        return {
            "project_id": self.project.id,
            "repo": self.project.repo_url or "",
            "default_branch": self.project.repo_default_branch or "main",
            "workspace": str(self.workspace) if self.workspace else "",
        }

    def portable(self, items: list[Item], name: str | None) -> Bundle:
        replacements = {value: "{{" + key + "}}" for key, value in self.defaults().items() if value}
        # Additional branch names and absolute paths are declared, never kept
        # as defaults which would reveal source-project details.
        for entry in items:
            if isinstance(entry.payload, PromotionPolicy):
                for step in entry.payload.flow:
                    for field in ("source", "target"):
                        branch = step.get(field)
                        if isinstance(branch, str) and branch not in replacements:
                            replacements[branch] = "{{branch_" + str(len(replacements)) + "}}"
            if isinstance(entry.payload, PolicyAgentSettings):
                for path in entry.payload.config.workspaces or []:
                    if Path(path).is_absolute() and path not in replacements:
                        replacements[path] = "{{path_" + str(len(replacements)) + "}}"
        portable = []
        used = set()
        for entry in items:
            validate_payload(entry.payload)
            data = transform(entry.payload.model_dump(mode="json"), replacements)
            # Re-templating a playbook's source updates its digest. Recompilation
            # against the destination registry happens in document review.
            if entry.type == "playbook":
                from src.playbooks.definition import source_digest

                data["artifact"]["source_hash"] = source_digest(data["source"])
            used.update(PLACEHOLDER.findall(json.dumps(data)))
            portable.append(item(entry.name, entry.original_scope, PAYLOAD.validate_python(data)))
        try:
            aq_version = version("agent-queue")
        except Exception:
            aq_version = "unknown"
        return Bundle(
            name=name or f"{self.project.name} policy",
            source_aq_version=aq_version,
            items=portable,
            placeholders=[
                Placeholder(name=key, description=key.replace("_", " ")) for key in sorted(used)
            ],
        )

    def materialize(
        self, bundle: Bundle, values: dict[str, str], *, require_values: bool = True
    ) -> list[Item]:
        known = {placeholder.name for placeholder in bundle.placeholders}
        if set(values) - known:
            raise ValueError("values name undeclared placeholders")
        inputs = {**self.defaults(), **values}
        missing = [key for key in known if not inputs.get(key)]
        if missing and require_values:
            raise ValueError("fill placeholders: " + ", ".join(sorted(missing)))

        def fill(value):
            if isinstance(value, str):
                return PLACEHOLDER.sub(
                    lambda match: (
                        inputs.get(match[1]) or match[0] if match[1] in known else match[0]
                    ),
                    value,
                )
            if isinstance(value, list):
                return [fill(entry) for entry in value]
            if isinstance(value, dict):
                return {fill(key): fill(entry) for key, entry in value.items()}
            return value

        result = []
        for entry in bundle.items:
            data = fill(entry.payload.model_dump(mode="json"))
            if entry.type == "playbook":
                from src.playbooks.definition import source_digest

                data["artifact"]["source_hash"] = source_digest(data["source"])
            payload = PAYLOAD.validate_python(data)
            if not missing:
                validate_payload(payload)
            result.append(item(entry.name, entry.original_scope, payload))
        return result

    def writes(self, entry: Item, scope: str) -> dict[Path, bytes]:
        payload = entry.payload
        root = self.root(scope)
        writes = {
            self._draft(entry.name, entry.type, scope): encoded(payload.model_dump(mode="json"))
        }
        if isinstance(payload, PolicyAgentSettings):
            relative = (
                f"overrides/{entry.name.removesuffix('.override')}.md"
                if payload.override
                else f"agent-types/{entry.name}/profile.md"
            )
            # Global profiles live directly in vault/agent-types.
            # Runtime profiles are global. Project settings stay outside the
            # retired layout which startup would promote into global scope.
            if scope == "global" and not payload.override:
                path = contained(self.vault, relative)
                existing = path.read_text() if path.is_file() else ""
                writes[path] = render_profile(entry.name, payload, existing).encode()
        elif isinstance(payload, TemplatePolicy):
            name = entry.name.removeprefix(payload.kind + ".")
            relative = (
                f"formulas/{name}.md"
                if payload.kind == "formula"
                else f"templates/{payload.kind}/{name}.md"
            )
            writes[contained(root, relative)] = payload.content.encode()
        elif isinstance(payload, CITestPolicy):
            target_root = self.workspace if scope == "project" and self.workspace else root
            for key, (relative, _model) in TEST_FILES.items():
                value = getattr(payload, key)
                if value is not None:
                    writes[contained(target_root, relative)] = yaml.safe_dump(
                        value.model_dump(mode="json"), sort_keys=False
                    ).encode()
            if payload.ci is not None:
                # Imported check policy is separate from account-bound trust
                # anchors. Existing anchors stay local to the repository.
                writes[contained(root, "policy/ci-checks.json")] = encoded(
                    payload.ci.model_dump(mode="json")
                )
        elif isinstance(payload, PlaybookPolicy):
            # Project placement must not collide with the globally unique
            # playbook identifier. All scope/identifier changes are re-reviewed.
            from src.playbooks.definition import source_digest

            identifier = entry.name if scope == "global" else f"{self.project.id}.{entry.name}"
            front, body = payload.source.split("---", 2)[1:]
            metadata = yaml.safe_load(front)
            metadata["id"] = identifier
            metadata["scope"] = "system" if scope == "global" else f"project:{self.project.id}"
            source = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
            artifact = dict(payload.artifact)
            artifact.update(
                id=identifier,
                scope={"type": "system"}
                if scope == "global"
                else {"type": "project", "project_id": self.project.id},
                source_hash=source_digest(source),
            )
            directory = contained(root, f"policy/pending-playbooks/{identifier}")
            writes[contained(directory, "source.md")] = source.encode()
            writes[contained(directory, "artifact.json")] = encoded(artifact)
            writes[contained(directory, "semantic-body.json")] = encoded(
                {"rules": artifact["rules"], "steps": artifact["steps"]}
            )
            writes[contained(root, f"playbooks/{identifier}.md")] = source.encode()
        return writes

    def diff(
        self,
        items: list[Item],
        selections: dict[str, Selection],
        only: list[str],
        skip: list[str],
        no_overwrite: bool,
        *,
        applying: bool = False,
    ) -> tuple[list[DiffItem], dict[str, dict[Path, bytes]]]:
        identifiers = {entry.id for entry in items}
        if (set(selections) | set(only) | set(skip)) - identifiers:
            raise ValueError("selection names an unknown item")
        planned = {}
        rows = []
        for entry in items:
            selection = selections.get(entry.id, Selection())
            omitted = (
                entry.id in skip
                or (bool(only) and entry.id not in only)
                or selection.scope == "skip"
            )
            scope = selection.scope if selection.scope != "skip" else "project"
            writes = self.writes(entry, scope)
            before = {str(path): path.read_text() if path.exists() else None for path in writes}
            after = {str(path): data.decode() for path, data in writes.items()}
            current = (
                checksum(before) if any(value is not None for value in before.values()) else None
            )
            status = "identical" if before == after else "will overwrite" if current else "new"
            selected = not omitted and (
                status != "will overwrite" or selection.overwrite and not no_overwrite
            )
            if entry.original_scope == "system" and (
                entry.id not in selections or "scope" not in selection.model_fields_set
            ):
                selected = False  # scope must be explicitly acknowledged
            if applying and selected and selection.expected_checksum != current:
                # A brand new item has no checksum. Existing items require the
                # exact preview fingerprint; there is no blind overwrite.
                raise ValueError(f"preview changed for {entry.id}; review its diff again")
            diff = (
                "".join(
                    difflib.unified_diff(
                        encoded(before).decode().splitlines(True),
                        encoded(after).decode().splitlines(True),
                        fromfile="current",
                        tofile="profile",
                    )
                )
                if status == "will overwrite"
                else ""
            )
            rows.append(
                DiffItem(
                    id=entry.id,
                    name=entry.name,
                    type=entry.type,
                    original_scope=entry.original_scope,
                    scope=selection.scope,
                    status=status,
                    selected=selected,
                    requires_scope_choice=entry.original_scope == "system",
                    current_checksum=current,
                    diff=diff,
                    destinations=[str(path) for path in writes],
                    state="pending review"
                    if entry.type == "playbook"
                    else "system template"
                    if scope == "global"
                    else "pending configuration"
                    if entry.type in {"promotion_flow", "routing", "agent_settings"}
                    else "copy",
                )
            )
            if selected and status != "identical":
                planned[entry.id] = writes
        destinations = [path for writes in planned.values() for path in writes]
        if len(set(destinations)) != len(destinations):
            raise ValueError("selected policy items write the same destination")
        return rows, planned


def apply_files(planned: dict[str, dict[Path, bytes]]) -> None:
    """Rollback all touched files on a failed local write; never activate policy."""
    backups = {}
    try:
        for writes in planned.values():
            for path, data in writes.items():
                backups[path] = path.read_bytes() if path.exists() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    except BaseException:
        for path, before in reversed(list(backups.items())):
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(before)
        raise
