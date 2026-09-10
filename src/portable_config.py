"""Portable, deliberately small configuration bundles.

Bundles are zip files rather than copies of an AQ data directory.  The latter
contains task history, project vaults, memory and often credentials.  This
module only serialises a reviewed allowlist of tuning sections plus the one
``profile.md`` file for each global profile.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from src.config_editor import read_raw_config, write_section
from src.profiles.parser import parse_profile

BUNDLE_FORMAT = "aq-portable-config"
BUNDLE_VERSION = 1
MANIFEST_NAME = "manifest.json"
CONFIG_NAME = "config.yaml"
PROFILE_PREFIX = "profiles/"

# This is intentionally a list of tuning knobs, not a list of every section
# which happens not to contain a token today. Adding a section is a conscious
# security review.
PORTABLE_CONFIG_SECTIONS = frozenset(
    {
        "agents_config",
        "integration",
        "scheduling",
        "pause_retry",
        "monitoring",
        "archive",
        "auto_task",
        "llm_logging",
        "pricing",
        "surface",
        "state_machine",
        "work_graph",
        "swarm",
        "resources",
        "metrics",
        "global_token_budget_daily",
        "max_daily_playbook_tokens",
        "max_concurrent_playbook_runs",
        "rate_limits",
    }
)

# A section that is only *partly* portable: the keys named here travel, and
# every other key in it is dropped and reported as excluded.  ``integration``
# is the case this exists for — its merge policy is exactly the kind of
# reviewed tuning a bundle should carry, while ``github_app`` and
# ``scratch_probe`` name an installation's own app id, repository and key
# paths.
PORTABLE_SECTION_KEYS: dict[str, frozenset[str]] = {
    "integration": frozenset(
        {
            "default_mode",
            "merge_ci_policy",
            "merge_required_checks",
            "merge_require_up_to_date",
        }
    ),
}

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
            if _SECRET_KEY.search(key) and not isinstance(child, (bool, int, float, type(None))):
                raise PortableBundleError(f"{child_location} is not portable (secret-like key)")
            if key.lower() in {"path", "directory", "dir", "root", "workspace"}:
                raise PortableBundleError(f"{child_location} is not portable (machine path key)")
            _assert_portable_value(child, child_location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_portable_value(child, f"{location}[{index}]")
    elif isinstance(value, str):
        if _ABSOLUTE_PATH.search(value):
            raise PortableBundleError(f"{location} is not portable (absolute or home-relative path)")
        if _SECRET_VALUE.search(value):
            raise PortableBundleError(f"{location} is not portable (credential-like value)")


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
    for key, value in selected.items():
        _assert_portable_value(value, key)
    return selected, tuple(sorted(excluded))


def bundle_preview(config_path: str, data_dir: str) -> dict[str, Any]:
    """Return the exact curated payload before an operator exports it."""
    config, excluded = curated_config(read_raw_config(config_path))
    profiles = _collect_profiles(data_dir)
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "config": config,
        "profiles": sorted(profiles),
        "excluded_sections": list(excluded),
        "excluded_data": [
            "project vaults and memory",
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
        text = source.read_text(encoding="utf-8")
        _validate_profile(profile_id, text)
        profiles[profile_id] = text
    return profiles


def export_bundle(destination: str, config_path: str, data_dir: str) -> dict[str, Any]:
    """Write a validated bundle after producing the same payload as preview."""
    preview = bundle_preview(config_path, data_dir)
    profiles = _collect_profiles(data_dir)
    target = Path(destination).expanduser().resolve()
    if target.suffix != ".aqbundle":
        raise PortableBundleError("bundle destination must end in .aqbundle")
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "config": CONFIG_NAME,
        "profiles": [f"{PROFILE_PREFIX}{profile_id}.md" for profile_id in sorted(profiles)],
    }
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".aqbundle", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            archive.writestr(CONFIG_NAME, yaml.safe_dump(preview["config"], sort_keys=True))
            for profile_id, text in profiles.items():
                archive.writestr(f"{PROFILE_PREFIX}{profile_id}.md", text)
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
            if manifest.get("version") != BUNDLE_VERSION:
                raise PortableBundleError(f"unsupported bundle version {manifest.get('version')!r}")
            profile_members = manifest.get("profiles")
            if (
                manifest.get("config") != CONFIG_NAME
                or not isinstance(profile_members, list)
                or not all(isinstance(member, str) for member in profile_members)
            ):
                raise PortableBundleError("bundle manifest is malformed")
            expected = {MANIFEST_NAME, CONFIG_NAME, *profile_members}
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
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise PortableBundleError(f"could not read portable bundle: {exc}") from exc
    return PortableBundle(config=config, profiles=profiles)


def inspect_bundle(source: str) -> dict[str, Any]:
    bundle = read_bundle(source)
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "config": bundle.config,
        "profiles": sorted(bundle.profiles),
        "excluded_data": ["project memory", "project vault content", "history", "logs", "credentials"],
    }


def validate_candidate_config(config_path: str, candidate: dict[str, Any]) -> list[str]:
    """Load ``candidate`` as a config file next to ``config_path``.

    Returns the load errors as strings — one entry per :class:`ConfigError`,
    so a caller can tell an error it introduced from one the file already
    had — or an empty list when the document is valid.  The temp file lives
    in the same directory so ``${ENV_VAR}`` resolution and any sibling
    ``config.{env}.yaml`` overlay behave as they will once the document is
    written for real.
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
    config_conflicts = sorted(set(bundle.config) & set(current))
    profile_conflicts: list[str] = []
    for profile_id in bundle.profiles:
        if (root / profile_id / "profile.md").exists() or await db.get_profile(profile_id):
            profile_conflicts.append(profile_id)
    profile_conflicts.sort()
    conflicts = {"config": config_conflicts, "profiles": profile_conflicts}
    if conflict == "error" and (config_conflicts or profile_conflicts):
        return {"error": "bundle conflicts with existing customization", "conflicts": conflicts}

    candidate = copy.deepcopy(current)
    selected_config = {
        key: value for key, value in bundle.config.items() if conflict == "replace" or key not in current
    }
    # A partly-portable section is *merged*, never replaced: the bundle only
    # ever carries its allowlisted keys, so writing it whole under
    # ``conflict=replace`` would delete the local-only rest of the section —
    # an operator's ``integration.github_app``, for one.
    for section in PORTABLE_SECTION_KEYS:
        incoming = selected_config.get(section)
        existing = current.get(section)
        if isinstance(incoming, dict) and isinstance(existing, dict):
            selected_config[section] = {**copy.deepcopy(existing), **incoming}
    candidate.update(selected_config)
    validation_errors = validate_candidate_config(config_path, candidate)
    if validation_errors:
        return {"error": "bundle configuration is invalid here", "validation_errors": validation_errors}
    selected_profiles = {
        profile_id: text
        for profile_id, text in bundle.profiles.items()
        if conflict == "replace" or profile_id not in profile_conflicts
    }
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
    }
    if dry_run:
        return result

    # Each section writer preserves comments and untouched user sections.
    for key, value in selected_config.items():
        write_section(config_path, key, value)
    from src.profiles.sync import sync_profile_text_to_db

    for profile_id, text in selected_profiles.items():
        target = root / profile_id / "profile.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and conflict == "replace":
            shutil.copy2(target, target.with_name("profile.md.bak"))
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as tmp:
            tmp.write(text)
            temp_profile = Path(tmp.name)
        os.replace(temp_profile, target)
        synced = await sync_profile_text_to_db(text, db, source_path=str(target), fallback_id=profile_id)
        if not synced.success:
            return {"error": f"profile {profile_id!r} could not be synced", "validation_errors": synced.errors or []}
    result["applied"] = bool(selected_config or selected_profiles)
    return result
