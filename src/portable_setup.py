"""Typed, secret-free setup metadata shared by configuration and policy bundles."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PoolSettings(Settings):
    name: str = ""
    enabled: bool = True
    min_active: int | None = Field(default=None, ge=0)
    max_active: int | None = Field(default=None, gt=0)
    min_per_project: int | None = Field(default=None, ge=0)
    max_claims_per_session: int | None = Field(default=None, ge=1)
    derived: bool = False
    extends: str = ""
    harness: str | None = None
    default_class: str = ""
    lifecycle: str = "task"

    @model_validator(mode="after")
    def valid_bounds(self):
        if self.max_active is not None and (self.min_active or 0) > self.max_active:
            raise ValueError("pool min_active exceeds max_active")
        if self.derived and not (self.extends and self.harness and self.default_class):
            raise ValueError("derived pool requires template, harness and intelligence class")
        return self

    def config(self) -> dict:
        return {key: getattr(self, key) for key in (
            "enabled", "min_active", "max_active", "min_per_project", "max_claims_per_session",
            "lifecycle",
        )}


class RepoSettings(Settings):
    id: str
    source_type: str = "clone"
    url: str = ""
    default_branch: str = "main"


class ProjectSettings(Settings):
    name: str
    credit_weight: float = Field(default=1.0, gt=0)
    max_concurrent_agents: int = Field(default=2, ge=1)
    status: str = "ACTIVE"
    budget_limit: int | None = Field(default=None, ge=0)
    repo_url: str = ""
    repo_default_branch: str = "main"
    assignment_playbook_id: str = "default-assignment-routing"
    integration_mode: str | None = None
    review_delegate_to: str | None = None
    preferred_provider: str | None = None
    repos: list[RepoSettings] = []
    integration_preferences: dict = {}

    @model_validator(mode="after")
    def valid_policy(self):
        from src.models import INTEGRATION_MODES

        if self.integration_mode is not None and self.integration_mode not in INTEGRATION_MODES:
            raise ValueError("invalid project integration mode")
        if self.review_delegate_to not in {None, "", "user", "supervisor"}:
            raise ValueError("invalid project review delegation")
        return self


class Setup(Settings):
    pools: dict[str, PoolSettings] = {}
    projects: dict[str, ProjectSettings] = {}
    # Only fixed, explicitly owned policy locations participate.
    project_files: dict[str, dict[str, str]] = {}
    system_files: dict[str, str] = {}


PROJECT_FILE_PATTERNS = (
    "overrides/*.md", "templates/spec/*.md", "templates/plan/*.md", "formulas/*.md",
    "playbooks/*.md", "policy/ci-checks.json",
    "policy/agent_settings/*.json", "policy/playbook/*.json", "policy/promotion_flow/*.json",
    "policy/ci_test/*.json", "policy/routing/*.json", "policy/template/*.json",
    "policy/intelligence_class/*.json", "policy/pool/*.json", "policy/project_settings/*.json",
    "mcp-servers/*.md", "workspace-kinds/*.md",
)
SYSTEM_FILE_PATTERNS = (
    "system/playbooks/*.md", "templates/spec/*.md", "templates/plan/*.md",
    "mcp-servers/*.md", "workspace-kinds/*.md",
)


def portable_url(url: str) -> str:
    """Remove URL credentials, query tokens and fragments; local repos need a new path."""
    if not url:
        return ""
    if url.startswith(("/", "~", "file:")) or (len(url) > 2 and url[1:3] in {":/", ":\\"}):
        return ""
    parsed = urlsplit(url)
    if parsed.scheme and parsed.netloc:
        authority = parsed.netloc.rsplit("@", 1)[-1]
        # The conventional SSH transport user is not a login credential.
        if parsed.scheme == "ssh" and parsed.username == "git":
            authority = "git@" + authority
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    return url


def validate_class_text(identifier: str, text: str) -> None:
    from src.intelligence_classes import _parse_text_result
    from src.portable_config import _assert_portable_value, _safe_profile_id, _SECRET_ASSIGNMENT

    _safe_profile_id(identifier)
    parsed, error = _parse_text_result(text, f"{identifier}.md")
    if parsed is None or parsed.id != identifier:
        raise ValueError(f"invalid intelligence class {identifier!r}: {error or 'id mismatch'}")
    _assert_portable_value(parsed.mapping, f"intelligence_classes.{identifier}")
    if _SECRET_ASSIGNMENT.search(text):
        raise ValueError(f"intelligence class {identifier!r} contains a secret-like field")


def collect_classes(data_dir: str) -> dict[str, str]:
    from src.portable_config import PortableBundleError, sanitize_policy_text

    result = {}
    root = Path(data_dir) / "vault/intelligence-classes"
    for path in sorted(root.glob("*.md")):
        if path.is_symlink() or path.resolve().parent != root.resolve():
            raise PortableBundleError("intelligence class path contains a symlink")
        text = sanitize_policy_text(path.read_text(encoding="utf-8"))
        validate_class_text(path.stem, text)
        result[path.stem] = text
    return result


def pool_settings(profile, text: str) -> PoolSettings:
    from src.profiles.parser import parse_profile

    parsed = parse_profile(text)
    return PoolSettings(
        name=profile.name, derived="derived" in parsed.frontmatter.tags,
        extends=parsed.frontmatter.extends, harness=profile.harness,
        default_class=profile.default_class, **{
            key: getattr(profile, key) for key in PoolSettings().config()
        },
    )


def pool_profile_text(identifier: str, pool: PoolSettings, existing: str = "") -> str:
    from src.profiles.catalog import CatalogProfile, render_rung_stub

    if not existing:
        if not pool.derived:
            raise ValueError(f"pool {identifier!r} requires its profile definition")
        existing = render_rung_stub(CatalogProfile(
            id=identifier, name=pool.name or identifier, harness=pool.harness,
            provider_id=pool.harness, intelligence_class=pool.default_class,
            source_profile_id=pool.extends,
        ))
    # JSON null explicitly resets bounds; update_config_keys uses None to delete,
    # so merge the Config block instead to retain an intentional unlimited value.
    from src.profiles.parser import parse_profile

    parsed = parse_profile(existing)
    if pool.name:
        import yaml

        header = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", existing, re.S)
        if header:
            metadata = yaml.safe_load(header[1])
            metadata["name"] = pool.name
            existing = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n" + existing[header.end():]
    config = {**parsed.config, **pool.config()}
    if pool.harness:
        config["harness"] = pool.harness
    if pool.default_class:
        config["default_class"] = pool.default_class
    block = "## Config\n```json\n" + json.dumps(
        config, indent=2
    ) + "\n```\n"
    pattern = re.compile(r"^## Config\s*\n.*?(?=^## |\Z)", re.M | re.S | re.I)
    if pattern.search(existing):
        return pattern.sub(lambda _match: block + "\n", existing)
    return existing.rstrip() + "\n\n" + block


async def project_settings(db, project) -> ProjectSettings:
    fields = {
        key: getattr(project, key) for key in ProjectSettings.model_fields
        if key not in {"repos", "integration_preferences"}
    }
    fields["status"] = project.status.value
    fields["repo_url"] = portable_url(project.repo_url)
    fields["repos"] = [RepoSettings(
        id=repo.id, url=portable_url(repo.url), default_branch=repo.default_branch,
        source_type=repo.source_type.value,
    ) for repo in await db.list_repos(project.id)]
    fields["integration_preferences"] = {
        "mode": project.hierarchical_integration_desired_mode,
        "policy": project.hierarchical_integration_policy,
        "repository_id": project.integration_repository_id,
    }
    return ProjectSettings(**fields)


def collect_policy_files(root: Path, patterns: tuple[str, ...]) -> dict[str, str]:
    from src.portable_config import sanitize_policy_text

    files = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("policy path contains a symlink")
            files[path.relative_to(root).as_posix()] = sanitize_policy_text(path.read_text())
    return files


async def collect_setup(db, data_dir: str) -> Setup:
    import asyncio
    from sqlalchemy import select
    from src.database.tables import projects
    from src.portable_config import sanitize_policy_text

    setup = Setup()
    vault = Path(data_dir) / "vault"
    for profile in await db.list_profiles():
        path = vault / "agent-types" / profile.id / "profile.md"
        if not path.is_file():
            raise ValueError(f"profile {profile.id!r} has no portable source definition")
        if path.is_symlink() or not path.resolve().is_relative_to(vault.resolve()):
            raise ValueError("profile path contains a symlink")
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        setup.pools[profile.id] = pool_settings(profile, text)
    for project in await db.list_projects():
        setup.projects[project.id] = await project_settings(db, project)
        root = vault / "projects" / project.id
        files = await asyncio.to_thread(collect_policy_files, root, PROJECT_FILE_PATTERNS)
        # Selection rules and required CI checks live in the repository, outside
        # the vault. Store their typed policy projection until local paths exist.
        from src.policy_profiles.service import PolicyFiles

        workspace = await db.get_project_workspace_path(project.id)
        adapter = PolicyFiles(vault, project, workspace)
        local = await asyncio.to_thread(adapter.read_local)
        for entry in local:
            if entry.type == "ci_test":
                files["policy/ci_test/tests.json"] = sanitize_policy_text(json.dumps(
                    entry.payload.model_dump(mode="json")
                ))
        async with db._engine.connect() as conn:
            flow = (await conn.execute(select(projects.c.promotion_flow).where(
                projects.c.id == project.id
            ))).scalar_one_or_none()
        if flow:
            files["policy/promotion_flow/flow.json"] = sanitize_policy_text(json.dumps({
                "type": "promotion_flow", "flow": flow,
            }))
        setup.project_files[project.id] = files
    setup.system_files = await asyncio.to_thread(collect_policy_files, vault, SYSTEM_FILE_PATTERNS)
    validate_setup(setup)
    return setup


def validate_setup(setup: Setup) -> None:
    from pathlib import PurePosixPath

    from src.models import ProjectStatus, RepoSourceType
    from src.portable_config import (
        _assert_portable_value, _safe_member_name, _safe_profile_id, sanitize_policy_text,
    )

    for identifier, pool in setup.pools.items():
        _safe_profile_id(identifier)
        if pool.extends:
            _safe_profile_id(pool.extends)
        if pool.lifecycle not in {"pool", "task", "named"}:
            raise ValueError("invalid profile lifecycle")
        if pool.default_class:
            _safe_profile_id(pool.default_class)
    for identifier, project in setup.projects.items():
        _safe_profile_id(identifier)
        ProjectStatus(project.status)
        if portable_url(project.repo_url) != project.repo_url:
            raise ValueError("project repository URL is not portable")
        for repo in project.repos:
            _safe_profile_id(repo.id)
            RepoSourceType(repo.source_type)
            if portable_url(repo.url) != repo.url:
                raise ValueError("repository URL is not portable")
    for identifier, files in setup.project_files.items():
        if identifier not in setup.projects:
            raise ValueError("project policy files require project settings")
        for name, text in files.items():
            _safe_member_name(name)
            if not any(PurePosixPath(name).match(pattern) for pattern in PROJECT_FILE_PATTERNS):
                raise ValueError(f"unrecognized project policy file {name!r}")
            if sanitize_policy_text(text) != text:
                raise ValueError("project policy contains non-portable settings")
    for name, text in setup.system_files.items():
        _safe_member_name(name)
        if not any(PurePosixPath(name).match(pattern) for pattern in SYSTEM_FILE_PATTERNS):
            raise ValueError(f"unrecognized system policy file {name!r}")
        if sanitize_policy_text(text) != text:
            raise ValueError("system policy contains non-portable settings")
    _assert_portable_value(setup.model_dump(mode="json"), "setup")
