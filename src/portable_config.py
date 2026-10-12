"""Versioned portable setup bundles, excluding credentials, local paths and history."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from src.config_editor import read_raw_config, write_section
from src.profiles.parser import parse_profile
from src.portable_setup import Setup, collect_classes, validate_class_text, validate_setup

BUNDLE_FORMAT = "aq-portable-config"
BUNDLE_VERSION = 2
MANIFEST_NAME = "manifest.json"
CONFIG_NAME = "config.yaml"
PROFILE_PREFIX = "profiles/"
CLASS_PREFIX = "intelligence-classes/"
SETUP_NAME = "setup.json"

# Explicitly reviewed setup sections. Nested credentials and machine bindings
# are removed without discarding the rest of the section's policy.
PORTABLE_CONFIG_SECTIONS = frozenset(
    {
        "agents_config", "integration", "scheduling", "monitoring",
        "archive", "auto_task", "llm_logging", "pricing",
        "surface", "state_machine", "work_graph",
        # Only limits and policy switches (artifact/result/snapshot sizes,
        # pending-event and retention policy) -- no path, no credential.
        "playbooks", "swarm", "resources", "metrics",
        "global_token_budget_daily", "max_daily_playbook_tokens",
        "max_concurrent_playbook_runs", "rate_limits",
        "database", "env", "profile", "validate_events", "messaging_platform", "discord",
        "reports", "llm", "supervisor", "health_check", "docs", "deploy", "logging",
        "memory", "knowledge", "mcp_server", "sessions", "worktrees", "streams", "security",
        "messages", "events", "supervisor_agent", "api_auth", "test_selection", "providers",
        "provider_failover", "routing", "graph_layout", "dashboard_server", "dashboard", "host_shell",
        "agent_profiles", "memory_extractor", "inbox", "planner", "plugins",
    }
)

# Partly portable sections export only these keys and report all others as
# exclusions. Integration merge policy travels; installation-specific GitHub
# App credentials and scratch-probe repositories and paths stay local.
PORTABLE_SECTION_KEYS: dict[str, frozenset[str]] = {
    "integration": frozenset(
        {
            "default_mode", "merge_ci_policy", "merge_required_checks",
            "merge_require_up_to_date", "owner_recovery_sweep", "publisher_stall_after",
            "reconciler_shadow", "reconciler_active", "git_first",
            "service_source_timeout_seconds", "service_item_timeout_seconds",
            "service_source_timeouts",
        }
    ),
}

# Reviewed exceptions at their actual nested location. Every other policy field
# in these sections travels, and an unknown secret/path still fails validation.
LOCAL_CONFIG_FIELDS = frozenset({
    "database.url", "database.schema_ahead_migrations",
    "discord.bot_token", "discord.guild_id", "discord.authorized_users", "discord.channel_id",
    "discord.escalation.mention_user_ids", "discord.escalation.mention_role_ids",
    "reports.morning.destination", "llm.api_key", "llm.fallback.api_key",
    "logging.log_file", "memory.embedding_api_key", "memory.milvus_token",
    "sessions.tmux_socket", "sessions.fake_script_file", "resources.cores",
    "resources.test_interpreters", "resources.jobs.test_database_url",
    "resources.jobs.matter_python", "resources.jobs.matter_capture_script",
    "resources.jobs.matter_editor", "resources.jobs.matter_gpu_id",
    "providers.claude.binary", "dashboard_server.public_url", "dashboard_server.tailscale_path",
    "dashboard.public_url", "dashboard.server.public_url", "dashboard.server.tailscale_path",
    "api_auth.trusted_dashboard_origins", "inbox.oauth_token_path", "inbox.client_secret",
})

_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|credential|private[_-]?key|auth)", re.I)
_SECRET_VALUE = re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|AKIA[0-9A-Z]{16})")
_SECRET_ASSIGNMENT = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential|private[_-]?key)\s*[:=]", re.I
)
_ABSOLUTE_PATH = re.compile(r"(?:^~(?:/|\\)|^/|^[A-Za-z]:[\\/]|^\\\\)")
_MAX_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_BUNDLE_BYTES = 8 * 1024 * 1024


class PortableBundleError(ValueError):
    """Raised before an unsafe or malformed bundle can be used."""


@dataclass(frozen=True)
class PortableBundle:
    config: dict[str, Any]
    profiles: dict[str, str]
    excluded_sections: tuple[str, ...] = ()
    intelligence_classes: dict[str, str] = field(default_factory=dict)
    setup: Setup = field(default_factory=Setup)
    version: int = BUNDLE_VERSION


def _safe_profile_id(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
        raise PortableBundleError(f"invalid profile id {profile_id!r}")
    return profile_id


def _safe_member_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or "\\" in name or any(part in ("", ".", "..") for part in path.parts):
        raise PortableBundleError(f"unsafe bundle member path {name!r}")
    if path.as_posix() != name:
        raise PortableBundleError(f"bundle member path is not normalized: {name!r}")
    return name


def _assert_portable_value(value: Any, location: str = "config") -> None:
    """Reject credentials and machine paths even in an allowlisted section."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PortableBundleError(f"{location} has a non-string key")
            child_location = f"{location}.{key}"
            # The secret-key heuristic is a substring match, so it fires on
            # perfectly ordinary numeric knobs — ``metrics.token_window_seconds``
            # and ``surface.context_cost_ceiling_tokens`` both contain "token".
            # A credential is a string (or a list of them); a number never is,
            # so exempting scalars keeps the heuristic aggressive on the values
            # that could actually carry one.
            from src.config_secrets import is_secret_key

            if is_secret_key(key) and not isinstance(child, (bool, int, float, type(None))):
                raise PortableBundleError(f"{child_location} is not portable (secret-like key)")
            # Repository-relative policy paths (selection rules, conftests,
            # templates) travel; absolute/home paths are rejected below.
            _assert_portable_value(child, child_location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_portable_value(child, f"{location}[{index}]")
    elif isinstance(value, str):
        if _ABSOLUTE_PATH.search(value):
            raise PortableBundleError(f"{location} is not portable (absolute or home-relative path)")
        if _SECRET_VALUE.search(value):
            raise PortableBundleError(f"{location} is not portable (credential-like value)")
        from src.config_secrets import SECRET_LINE_PATTERNS

        if any(pattern.search(value) for pattern in SECRET_LINE_PATTERNS):
            raise PortableBundleError(f"{location} is not portable (credential-like value)")


def _without_local_fields(value: Any, location: str, excluded: set[str]) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            dotted = f"{location}.{key}"
            if dotted in LOCAL_CONFIG_FIELDS or (
                isinstance(child, str) and _ABSOLUTE_PATH.search(child)
            ):
                excluded.add(dotted)
            else:
                cleaned = _without_local_fields(child, dotted, excluded)
                # An omitted path list must not become an explicit request to
                # clear destination paths during a replacement import.
                if isinstance(child, list) and child and cleaned == []:
                    excluded.add(dotted)
                else:
                    result[key] = cleaned
        return result
    if isinstance(value, list):
        result = []
        for index, child in enumerate(value):
            if isinstance(child, str) and _ABSOLUTE_PATH.search(child):
                excluded.add(f"{location}[{index}]")
            else:
                result.append(_without_local_fields(child, location, excluded))
        return result
    return copy.deepcopy(value)


def sanitize_policy_text(text: str) -> str:
    """Remove structured credentials and machine paths from policy/profile sources."""
    from src.config_secrets import is_secret_key, SECRET_LINE_PATTERNS

    def portable(value):
        if isinstance(value, dict):
            return {
                key: portable(child) for key, child in value.items()
                if not is_secret_key(key)
                and key not in {"env", "operator_logins", "git_identity_name", "git_identity_email"}
                and not (isinstance(child, str) and _ABSOLUTE_PATH.search(child))
                and key not in {"source_path", "checkout_base_path", "workspace_path"}
            }
        if isinstance(value, list):
            return [portable(child) for child in value if not (
                isinstance(child, str) and _ABSOLUTE_PATH.search(child)
            )]
        return value

    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        return json.dumps(portable(raw), indent=2, sort_keys=True) + "\n"
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", text, re.S)
    if frontmatter:
        header = yaml.safe_load(frontmatter[1])
        if isinstance(header, dict):
            text = "---\n" + yaml.safe_dump(portable(header), sort_keys=False) + "---\n" + text[frontmatter.end():]
    text = re.sub(
        r"(^```json\s*\n)(.*?)(^```)",
        lambda match: match[1] + json.dumps(portable(json.loads(match[2])), indent=2) + "\n" + match[3],
        text, flags=re.M | re.S,
    )
    # Literal credentials in prose are excluded too; names of environment
    # references are policy and may remain in instructions.
    text = "\n".join(line for line in text.split("\n") if not any(
        pattern.search(line) for pattern in SECRET_LINE_PATTERNS
    ))
    # Embedded home/workspace paths in prose also identify the source machine.
    text = re.sub(
        r"(?<![\w:/])(?:~[/\\]|/(?:home|Users|root|tmp|var|opt|srv|mnt|media)/|[A-Za-z]:[/\\])[^\s\"'`)}\]]+",
        "[local path omitted]", text,
    )
    return text


def _validate_profile(profile_id: str, text: str) -> None:
    _safe_profile_id(profile_id)
    _assert_portable_value(text, f"profiles.{profile_id}")
    if _SECRET_ASSIGNMENT.search(text):
        raise PortableBundleError(f"profile {profile_id!r} contains a secret-like field")
    parsed = parse_profile(text)
    if not parsed.is_valid:
        raise PortableBundleError(f"profile {profile_id!r} is invalid: {'; '.join(parsed.errors)}")
    if parsed.frontmatter.id != profile_id:
        raise PortableBundleError(
            f"profile {profile_id!r} frontmatter id must match its normalized bundle path"
        )
    for section in parsed.sections.values():
        if section.json_data is not None:
            _assert_portable_value(section.json_data, f"profiles.{profile_id}.{section.heading}")


def curated_config(raw: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Split a raw config into the portable part and what was left behind.

    The second element names everything dropped: whole sections outside
    :data:`PORTABLE_CONFIG_SECTIONS`, and — for a section with a key
    allowlist — the individual dotted keys inside it that do not travel.
    Export reports those to the operator; import refuses a bundle that
    carries any of them, so a hand-built bundle cannot smuggle one in.
    """
    if not isinstance(raw, dict):
        raise PortableBundleError("configuration must be a mapping")
    selected = {key: copy.deepcopy(raw[key]) for key in PORTABLE_CONFIG_SECTIONS if key in raw}
    excluded = set(raw) - PORTABLE_CONFIG_SECTIONS
    for section, allowed in PORTABLE_SECTION_KEYS.items():
        body = selected.get(section)
        if not isinstance(body, dict):
            continue
        for key in sorted(set(body) - allowed):
            excluded.add(f"{section}.{key}")
            del body[key]
    for key, value in list(selected.items()):
        selected[key] = value = _without_local_fields(value, key, excluded)
        _assert_portable_value(value, key)
        if value == {}:
            del selected[key]
    return selected, tuple(sorted(excluded))


def bundle_preview(config_path: str, data_dir: str, *, setup: Setup | None = None) -> dict[str, Any]:
    """Return the exact curated payload before an operator exports it."""
    config, excluded = curated_config(read_raw_config(config_path))
    profiles = _collect_profiles(data_dir)
    classes = collect_classes(data_dir)
    setup = setup or Setup()
    validate_setup(setup)
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "config": config,
        "profiles": sorted(profiles),
        "intelligence_classes": sorted(classes),
        "pools": sorted(setup.pools),
        "projects": sorted(setup.projects),
        "project_files": {key: sorted(files) for key, files in setup.project_files.items()},
        "system_files": sorted(setup.system_files),
        "excluded_sections": list(excluded),
        "excluded_data": [
            "project memory, notes and research",
            "task/session history and logs",
            "provider credentials and secrets",
            "machine and repository paths",
        ],
    }


def _collect_profiles(data_dir: str) -> dict[str, str]:
    root = Path(data_dir) / "vault" / "agent-types"
    if not root.exists():
        return {}
    profiles: dict[str, str] = {}
    for source in sorted(root.glob("*/profile.md")):
        profile_id = _safe_profile_id(source.parent.name)
        if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
            raise PortableBundleError("profile path contains a symlink")
        text = sanitize_policy_text(source.read_text(encoding="utf-8"))
        _validate_profile(profile_id, text)
        if "derived" in parse_profile(text).frontmatter.tags:
            continue
        profiles[profile_id] = text
    return profiles


def export_bundle(
    destination: str, config_path: str, data_dir: str, *, setup: Setup | None = None,
) -> dict[str, Any]:
    """Write a validated bundle after producing the same payload as preview."""
    setup = setup or Setup()
    preview = bundle_preview(config_path, data_dir, setup=setup)
    profiles = _collect_profiles(data_dir)
    classes = collect_classes(data_dir)
    target = Path(destination).expanduser().resolve()
    if target.suffix != ".aqbundle":
        raise PortableBundleError("bundle destination must end in .aqbundle")
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "config": CONFIG_NAME,
        "profiles": [f"{PROFILE_PREFIX}{profile_id}.md" for profile_id in sorted(profiles)],
        "intelligence_classes": [f"{CLASS_PREFIX}{identifier}.md" for identifier in sorted(classes)],
        "setup": SETUP_NAME,
    }
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".aqbundle", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            archive.writestr(CONFIG_NAME, yaml.safe_dump(preview["config"], sort_keys=True))
            for profile_id, text in profiles.items():
                archive.writestr(f"{PROFILE_PREFIX}{profile_id}.md", text)
            for identifier, text in classes.items():
                archive.writestr(f"{CLASS_PREFIX}{identifier}.md", text)
            archive.writestr(SETUP_NAME, setup.model_dump_json(indent=2))
        # Apply the reader's validation and size limits before publishing too.
        read_bundle(str(tmp_path))
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)
    return {**preview, "path": str(target), "written": True}


def read_bundle(source: str) -> PortableBundle:
    """Read a bundle with strict archive, schema, secret, and profile checks."""
    source_path = Path(source).expanduser().resolve()
    try:
        with zipfile.ZipFile(source_path) as archive:
            infos = archive.infolist()
            names = [_safe_member_name(info.filename) for info in infos]
            if len(names) != len(set(names)):
                raise PortableBundleError("bundle contains duplicate member names")
            if any(info.is_dir() or info.file_size > _MAX_MEMBER_BYTES for info in infos):
                raise PortableBundleError("bundle contains an invalid or oversized member")
            if sum(info.file_size for info in infos) > _MAX_BUNDLE_BYTES:
                raise PortableBundleError("bundle is too large")
            if MANIFEST_NAME not in names or CONFIG_NAME not in names:
                raise PortableBundleError("bundle must contain manifest.json and config.yaml")
            manifest = json.loads(archive.read(MANIFEST_NAME))
            if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
                raise PortableBundleError("not an AQ portable configuration bundle")
            version = manifest.get("version")
            if type(version) is not int or version not in {1, BUNDLE_VERSION}:
                raise PortableBundleError(f"unsupported bundle version {manifest.get('version')!r}")
            profile_members = manifest.get("profiles")
            class_members = manifest.get("intelligence_classes", []) if version == 2 else []
            if (
                manifest.get("config") != CONFIG_NAME
                or not isinstance(profile_members, list)
                or not all(isinstance(member, str) for member in profile_members)
                or not isinstance(class_members, list)
                or not all(isinstance(member, str) for member in class_members)
                or (version == 2 and manifest.get("setup") != SETUP_NAME)
            ):
                raise PortableBundleError("bundle manifest is malformed")
            expected = {MANIFEST_NAME, CONFIG_NAME, *profile_members, *class_members}
            if version == 2:
                expected.add(SETUP_NAME)
            if set(names) != expected:
                raise PortableBundleError("bundle contains unmanifested or missing members")
            raw_config = yaml.safe_load(archive.read(CONFIG_NAME)) or {}
            config, excluded = curated_config(raw_config)
            if excluded:
                raise PortableBundleError(f"bundle contains non-portable config sections: {', '.join(excluded)}")
            profiles: dict[str, str] = {}
            for member in profile_members:
                _safe_member_name(member)
                if not isinstance(member, str) or not member.startswith(PROFILE_PREFIX) or not member.endswith(".md"):
                    raise PortableBundleError(f"invalid profile member {member!r}")
                profile_id = _safe_profile_id(member[len(PROFILE_PREFIX) : -3])
                if profile_id in profiles:
                    raise PortableBundleError(f"duplicate profile {profile_id!r}")
                try:
                    text = archive.read(member).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise PortableBundleError(f"profile {profile_id!r} is not UTF-8") from exc
                _validate_profile(profile_id, text)
                profiles[profile_id] = text
            classes = {}
            for member in class_members:
                _safe_member_name(member)
                if not member.startswith(CLASS_PREFIX) or not member.endswith(".md"):
                    raise PortableBundleError(f"invalid intelligence class member {member!r}")
                identifier = _safe_profile_id(member[len(CLASS_PREFIX):-3])
                if identifier in classes:
                    raise PortableBundleError(f"duplicate intelligence class {identifier!r}")
                text = archive.read(member).decode("utf-8")
                validate_class_text(identifier, text)
                classes[identifier] = text
            setup = Setup.model_validate_json(archive.read(SETUP_NAME)) if version == 2 else Setup()
            validate_setup(setup)
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise PortableBundleError(f"could not read portable bundle: {exc}") from exc
    except (ValueError, UnicodeError) as exc:
        raise PortableBundleError(str(exc)) from exc
    return PortableBundle(config=config, profiles=profiles, intelligence_classes=classes,
                          setup=setup, version=version)


def inspect_bundle(source: str) -> dict[str, Any]:
    bundle = read_bundle(source)
    return {
        "format": BUNDLE_FORMAT,
        "version": bundle.version,
        "config": bundle.config,
        "profiles": sorted(bundle.profiles),
        "intelligence_classes": sorted(bundle.intelligence_classes),
        "pools": sorted(bundle.setup.pools),
        "projects": sorted(bundle.setup.projects),
        "system_files": sorted(bundle.setup.system_files),
        "project_files": {key: sorted(files) for key, files in bundle.setup.project_files.items()},
        "excluded_data": ["project memory", "notes", "history", "logs", "credentials", "paths"],
    }


def validate_candidate_config(config_path: str, candidate: dict[str, Any]) -> list[str]:
    """Validate beside ``config_path`` and return individual load errors.

    The sibling temporary file preserves environment expansion and config
    overlays. An empty list means the candidate is valid.
    """
    from src.config import ConfigValidationError, load_config

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", encoding="utf-8", dir=str(Path(config_path).parent), delete=False
    ) as tmp:
        yaml.safe_dump(candidate, tmp, sort_keys=False)
        temp_path = tmp.name
    try:
        load_config(temp_path)
    except ConfigValidationError as exc:
        return list(exc.errors)
    except Exception as exc:
        return [str(exc)]
    finally:
        Path(temp_path).unlink(missing_ok=True)
    return []


async def import_bundle(
    source: str,
    *,
    config_path: str,
    data_dir: str,
    db: Any,
    conflict: str = "keep",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Preflight then import a bundle, never replacing customisation by default."""
    if conflict not in {"keep", "replace", "error"}:
        return {"error": "conflict must be one of: keep, replace, error"}
    bundle = read_bundle(source)
    current = read_raw_config(config_path)
    root = Path(data_dir) / "vault" / "agent-types"
    vault = root.parent
    config_conflicts = sorted(set(bundle.config) & set(current))
    profile_conflicts: list[str] = []
    for profile_id in bundle.profiles:
        if (root / profile_id / "profile.md").exists() or await db.get_profile(profile_id):
            profile_conflicts.append(profile_id)
    profile_conflicts.sort()
    conflicts = {"config": config_conflicts, "profiles": profile_conflicts}
    class_conflicts = sorted(identifier for identifier in bundle.intelligence_classes
                             if (vault / CLASS_PREFIX / f"{identifier}.md").exists())
    pool_conflicts = sorted([identifier for identifier in bundle.setup.pools
                            if (root / identifier / "profile.md").exists()
                            or await db.get_profile(identifier)])
    project_conflicts = sorted([identifier for identifier in bundle.setup.projects
                               if await db.get_project(identifier)])
    file_conflicts = sorted(f"{identifier}/{name}" for identifier, files in
                            bundle.setup.project_files.items() for name in files
                            if (vault / "projects" / identifier / name).exists())
    system_conflicts = sorted(name for name in bundle.setup.system_files if (vault / name).exists())
    for name, incoming, collisions in (
        ("intelligence_classes", bundle.intelligence_classes, class_conflicts),
        ("pools", bundle.setup.pools, pool_conflicts),
        ("projects", bundle.setup.projects, project_conflicts),
        ("project_files", bundle.setup.project_files, file_conflicts),
        ("system_files", bundle.setup.system_files, system_conflicts),
    ):
        if incoming:
            conflicts[name] = collisions
    if conflict == "error" and any(conflicts.values()):
        return {"error": "bundle conflicts with existing customization", "conflicts": conflicts}

    candidate = copy.deepcopy(current)
    selected_config = {
        key: value for key, value in bundle.config.items() if conflict == "replace" or key not in current
    }
    # A partly-portable section is *merged*, never replaced: the bundle only
    # ever carries its allowlisted keys, so writing it whole under
    # ``conflict=replace`` would delete the local-only rest of the section —
    # an operator's ``integration.github_app``, for one.
    for section, incoming in selected_config.items():
        existing = current.get(section)
        if isinstance(incoming, dict) and isinstance(existing, dict):
            selected_config[section] = _merge_mapping(existing, incoming)
    candidate.update(selected_config)
    validation_errors = validate_candidate_config(config_path, candidate)
    if validation_errors:
        return {"error": "bundle configuration is invalid here", "validation_errors": validation_errors}
    selected_profiles = {
        profile_id: text
        for profile_id, text in bundle.profiles.items()
        if conflict == "replace" or profile_id not in profile_conflicts
    }
    selected_classes = {
        identifier: text for identifier, text in bundle.intelligence_classes.items()
        if conflict == "replace" or identifier not in class_conflicts
    }
    selected_pools = {
        identifier: pool for identifier, pool in bundle.setup.pools.items()
        if conflict == "replace" or identifier not in pool_conflicts
    }
    selected_projects = {
        identifier: project for identifier, project in bundle.setup.projects.items()
        if conflict == "replace" or identifier not in project_conflicts
    }
    from src.portable_setup import pool_profile_text
    from src.profiles.inheritance import resolve_inheritance
    from src.policy_profiles.service import contained

    writes = {}
    for name, text in bundle.setup.system_files.items():
        if conflict == "replace" or name not in system_conflicts:
            writes[contained(vault, name)] = text
    for identifier, text in selected_classes.items():
        writes[contained(vault, f"{CLASS_PREFIX}{identifier}.md")] = text
    for identifier, pool in selected_pools.items():
        path = contained(root, f"{identifier}/profile.md")
        text = selected_profiles.get(identifier)
        if text is None and path.is_file():
            text = path.read_text(encoding="utf-8")
        selected_profiles[identifier] = pool_profile_text(identifier, pool, text or "")
    for identifier, text in list(selected_profiles.items()):
        path = contained(root, f"{identifier}/profile.md")
        if conflict == "replace" and path.is_file():
            selected_profiles[identifier] = _preserve_local_profile_settings(
                text, path.read_text(encoding="utf-8")
            )

    def load_template(identifier):
        path = contained(root, f"{_safe_profile_id(identifier)}/profile.md")
        text = selected_profiles.get(identifier)
        if text is None and path.is_file():
            text = path.read_text(encoding="utf-8")
        return parse_profile(text) if text is not None else None

    for identifier, text in selected_profiles.items():
        parsed, errors = resolve_inheritance(parse_profile(text), load_template)
        if not parsed.is_valid or errors:
            raise PortableBundleError("invalid profile inheritance: " + "; ".join(parsed.errors + errors))
        class_id = parsed.config.get("default_class")
        if class_id and (bundle.intelligence_classes or selected_pools):
            if class_id not in selected_classes and not (vault / CLASS_PREFIX / f"{class_id}.md").is_file():
                raise PortableBundleError(f"profile {identifier!r} requires intelligence class {class_id!r}")
        writes[contained(root, f"{identifier}/profile.md")] = text
    for identifier, project in selected_projects.items():
        previous = await db.get_project(identifier)
        from src.portable_setup import portable_url

        if previous and project.repo_url and portable_url(previous.repo_url) != project.repo_url:
            raise PortableBundleError("import cannot reassign an existing project repository")
        for repo in project.repos:
            existing = await db.get_repo(repo.id)
            if existing and (existing.project_id != identifier or (
                repo.url and portable_url(existing.url) != repo.url
            )):
                raise PortableBundleError(f"repository {repo.id!r} is already bound differently")
    for identifier, files in bundle.setup.project_files.items():
        for name, text in files.items():
            if conflict == "replace" or f"{identifier}/{name}" not in file_conflicts:
                writes[contained(vault, f"projects/{identifier}/{name}")] = text
    result = {
        "applied": False,
        "dry_run": dry_run,
        "conflict": conflict,
        "conflicts": conflicts,
        "config_sections": sorted(selected_config),
        "profiles": sorted(selected_profiles),
        "skipped_config_sections": config_conflicts if conflict == "keep" else [],
        "skipped_profiles": profile_conflicts if conflict == "keep" else [],
        "validation_errors": [],
        "intelligence_classes": sorted(selected_classes),
        "pools": sorted(selected_pools),
        "projects": sorted(selected_projects),
        "project_files": sorted(str(path.relative_to(vault)) for path in writes
                                if path.is_relative_to(vault / "projects")),
        "skipped_intelligence_classes": class_conflicts if conflict == "keep" else [],
        "skipped_pools": pool_conflicts if conflict == "keep" else [],
        "skipped_projects": project_conflicts if conflict == "keep" else [],
        "system_files": sorted(set(bundle.setup.system_files) - (
            set(system_conflicts) if conflict == "keep" else set()
        )),
        "pending_configuration": ["repository workspace paths and credentials"]
        if selected_projects else [],
    }
    policy_sources = [str(path.relative_to(vault)) for path in writes
                      if path.is_relative_to(vault) and any(
                          part in {"playbooks", "mcp-servers", "workspace-kinds", "policy"}
                          for part in path.relative_to(vault).parts
                      )]
    result["pending_policy_sources"] = sorted(policy_sources)
    if dry_run:
        return result

    # Each section writer preserves comments and untouched user sections.
    for key, value in selected_config.items():
        write_section(config_path, key, value)
    from src.profiles.sync import sync_profile_text_to_db

    # All templates/classes land before any profile sync, independent of ids.
    for target, text in writes.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and conflict == "replace":
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as tmp:
            tmp.write(text)
            temp_profile = Path(tmp.name)
        os.replace(temp_profile, target)
    for profile_id, text in selected_profiles.items():
        target = root / profile_id / "profile.md"
        synced = await sync_profile_text_to_db(text, db, source_path=str(target), fallback_id=profile_id)
        if not synced.success:
            return {"error": f"profile {profile_id!r} could not be synced", "validation_errors": synced.errors or []}
    await _apply_project_settings(selected_projects, db, vault, result)
    result["applied"] = bool(selected_config or writes or selected_projects)
    return result


async def _apply_project_settings(selected_projects, db, vault, result) -> None:
    """Apply portable project fields; leave integration trust as a pending draft."""
    from src.models import Project, ProjectStatus, RepoConfig, RepoSourceType

    for identifier, settings in selected_projects.items():
        fields = _project_fields(settings)
        fields["status"] = ProjectStatus(fields["status"])
        previous = await db.get_project(identifier)
        if previous:
            # The existing repository may embed a destination-local credential
            # or name a local directory; neither is replaced by an omission.
            fields["repo_url"] = previous.repo_url
            await db.update_project(identifier, **fields)
        else:
            await db.create_project(Project(id=identifier, **fields))
        for repo in settings.repos:
            if await db.get_repo(repo.id):
                await db.update_repo(repo.id, default_branch=repo.default_branch)
            else:
                await db.create_repo(RepoConfig(
                    id=repo.id, project_id=identifier, source_type=RepoSourceType(repo.source_type),
                    url=repo.url, default_branch=repo.default_branch,
                ))
        _stage_project_preferences(identifier, settings, vault, result)


def _project_fields(settings) -> dict:
    return settings.model_dump(exclude={"repos", "integration_preferences"})


def _stage_project_preferences(identifier, settings, vault, result) -> None:
    """Keep desired integration policy pending destination-local trust and rollout."""
    from src.policy_profiles.service import contained

    if settings.integration_preferences.get("mode") != "disabled":
        target = contained(vault, f"projects/{identifier}/policy/project_settings/settings.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "type": "project_settings", "settings": settings.model_dump(mode="json"),
        }, indent=2) + "\n")
        result["pending_configuration"].append(f"{identifier}: integration trust and rollout")


def _merge_mapping(existing: dict, incoming: dict) -> dict:
    result = copy.deepcopy(existing)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_mapping(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _preserve_local_profile_settings(incoming: str, existing: str) -> str:
    """Keep destination-only JSON settings without copying any source secret."""
    old = parse_profile(existing)
    clean = parse_profile(sanitize_policy_text(existing))
    new = parse_profile(incoming)
    for key, section in old.sections.items():
        data = section.json_data
        portable = clean.sections.get(key)
        if not isinstance(data, dict) or portable is None or data == portable.json_data:
            continue
        replacement = new.sections.get(key)
        if replacement is not None and isinstance(replacement.json_data, dict):
            merged = _merge_mapping(data, replacement.json_data)
        else:
            merged = data
        block = f"## {section.heading}\n```json\n" + json.dumps(merged, indent=2) + "\n```\n"
        pattern = re.compile(r"^## " + re.escape(section.heading) + r"\s*\n.*?(?=^## |\Z)", re.M | re.S)
        if pattern.search(incoming):
            incoming = pattern.sub(lambda _match: block + "\n", incoming)
        else:
            incoming = incoming.rstrip() + "\n\n" + block
    return incoming
