"""Provider-aware shipped worker-profile catalog.

Vault profile markdown is user-owned once seeded, but a profile is not a
promise that its matching CLI can run on this host.  This module derives the
provider-specific worker ladder from canonical templates, observes the
existing secret-free login probes, and records which generated profiles are
safe defaults.  Rerunning ``aq install`` after login, removal, or repair thus
refreshes routing eligibility without erasing operator profile choices.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.install.logins import AuthProbe, login_instructions, provider_logins
from src.install.platform import PlatformFacts
from src.profiles.parser import parse_profile
from src.profiles.retired_defaults import retired_default_ids

logger = logging.getLogger(__name__)

ACTIVATION_FILENAME = "profile-activation.json"
ACTIVATION_SCHEMA_VERSION = 1
WORKER_TIERS: tuple[tuple[str, str], ...] = (
    ("fast-medium", "Fast (Medium)"),
    ("standard-medium", "Standard (Medium)"),
    ("deep-high", "Deep (High)"),
)
SUPPORTED_PROVIDER_IDS = frozenset({"claude", "codex", "gemini"})


@dataclass(frozen=True, slots=True)
class CatalogProfile:
    id: str
    provider_id: str
    harness: str
    intelligence_class: str
    name: str
    source_profile_id: str


@dataclass(frozen=True, slots=True)
class ProfileActivation:
    profile: CatalogProfile
    active: bool
    reason: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.profile.provider_id,
            "harness": self.profile.harness,
            "active": self.active,
            "reason": self.reason,
            "remediation": self.remediation,
        }


def shipped_profile_catalog(
    providers: Iterable[str] = tuple(sorted(SUPPORTED_PROVIDER_IDS)),
) -> tuple[CatalogProfile, ...]:
    """Return the provider-explicit worker ladder in stable order."""
    entries: list[CatalogProfile] = []
    for provider_id in providers:
        if provider_id not in SUPPORTED_PROVIDER_IDS:
            raise ValueError(f"unsupported profile-catalog provider: {provider_id}")
        title = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}[provider_id]
        for tier, display_tier in WORKER_TIERS:
            entries.append(
                CatalogProfile(
                    id=f"worker-{tier}-{provider_id}", provider_id=provider_id,
                    harness=provider_id, intelligence_class=tier,
                    name=f"{title} · {display_tier}",
                    source_profile_id=f"worker-{tier}-claude",
                )
            )
    return tuple(entries)


def evaluate_catalog(
    probes: Iterable[AuthProbe], *, facts: PlatformFacts | None = None, interactive: bool = True,
) -> tuple[ProfileActivation, ...]:
    """Map provider installation/readiness facts onto every catalog entry."""
    del facts  # The installer admits supported macOS/WSL hosts before this point.
    by_provider = {probe.provider_id: probe for probe in probes}
    logins = {login.provider_id: login for login in provider_logins()}
    result: list[ProfileActivation] = []
    for profile in shipped_profile_catalog():
        probe = by_provider.get(profile.provider_id)
        login = logins[profile.provider_id]
        if probe is None or not probe.installed:
            result.append(ProfileActivation(
                profile, False, f"{login.title} is not installed",
                f"Install {login.title}, then rerun `aq install` to refresh profile eligibility. {login.docs_url}",
            ))
        elif not probe.authenticated:
            result.append(ProfileActivation(
                profile, False, f"{login.title} is installed but not authenticated",
                login_instructions(login, probe, interactive=interactive),
            ))
        else:
            result.append(ProfileActivation(profile, True, "provider CLI is installed and authenticated"))
    return tuple(result)


def activation_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / "vault" / ACTIVATION_FILENAME


def active_catalog_profile_ids(data_dir: str | os.PathLike[str]) -> set[str] | None:
    """Read active generated profile ids, or ``None`` before first refresh."""
    try:
        payload = json.loads(activation_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema_version") != ACTIVATION_SCHEMA_VERSION:
        return None
    profiles = payload.get("profiles")
    if not isinstance(profiles, Mapping):
        return None
    return {
        str(profile_id) for profile_id, detail in profiles.items()
        if isinstance(detail, Mapping) and detail.get("active") is True
    }


def refresh_catalog_profiles(
    data_dir: str | os.PathLike[str], probes: Iterable[AuthProbe], *,
    facts: PlatformFacts | None = None, interactive: bool = True,
) -> dict[str, list[str]]:
    """Persist eligibility and seed newly-ready worker profiles.

    Existing files are left intact.  An inactive profile is retained if it is
    already present, while the activation record removes it from default
    routing until its provider is repaired.
    """
    activations = evaluate_catalog(probes, facts=facts, interactive=interactive)
    root = Path(data_dir)
    defaults_root = Path(__file__).with_name("defaults")
    profiles_root = root / "vault" / "agent-types"
    retired = retired_default_ids(str(root))
    existing_routes = _existing_profile_routes(profiles_root)
    result = {"created": [], "skipped": [], "inactive": [], "retired": [], "duplicates": []}
    for activation in activations:
        profile = activation.profile
        if not activation.active:
            result["inactive"].append(profile.id)
            continue
        if profile.id in retired:
            result["retired"].append(profile.id)
            continue
        destination = profiles_root / profile.id / "profile.md"
        if destination.exists():
            result["skipped"].append(profile.id)
            continue
        route = (profile.harness, profile.intelligence_class)
        if route in existing_routes:
            result["duplicates"].append(profile.id)
            logger.info(
                "catalog profile %s not seeded; %s already serves harness=%s default_class=%s",
                profile.id, existing_routes[route], profile.harness, profile.intelligence_class,
            )
            continue
        source = defaults_root / profile.source_profile_id / "profile.md"
        text = source.read_text(encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_materialize_profile(text, profile), encoding="utf-8")
        result["created"].append(profile.id)
        existing_routes[route] = profile.id
        logger.info(
            "catalog profile created: %s (harness=%s default_class=%s)",
            profile.id, profile.harness, profile.intelligence_class,
        )
    _write_activation(
        activation_path(root),
        {"schema_version": ACTIVATION_SCHEMA_VERSION,
         "profiles": {activation.profile.id: activation.to_dict() for activation in activations}},
    )
    return result


def _materialize_profile(template: str, profile: CatalogProfile) -> str:
    """Render a provider sibling without maintaining nine prompt copies."""
    title = profile.name.split(" · ")[0]
    text = template.replace(profile.source_profile_id, profile.id)
    text = text.replace('"Claude ·', f'"{title} ·').replace("# Claude ·", f"# {title} ·")
    text = text.replace('"harness": "claude"', f'"harness": "{profile.harness}"')
    text = text.replace(
        "It ships on the `claude` harness at intelligence class",
        f"It ships on the `{profile.harness}` harness at intelligence class",
    )
    if profile.harness != "claude":
        text = text.replace(
            "resolves to a concrete Anthropic model.",
            f"resolves to a concrete {profile.harness.title()} model.",
        )
        text = text.replace(
            "A Codex or Gemini equivalent is a\nseparate profile with its own `-codex` / `-gemini` id — repointing this\nprofile's harness would make its id stop describing what actually runs.",
            "Other harnesses use their own provider-specific profile IDs; repointing this\nprofile's harness would make its id stop describing what actually runs.",
        )
    return text


def _existing_profile_routes(profiles_root: Path) -> dict[tuple[str, str], str]:
    """Return the first valid vault profile for each harness/class route.

    Catalog siblings are conveniences, never a reason to recreate an
    operator's already-working route.  Lifecycle is deliberately excluded:
    a task-lifecycle profile still proves that this host has an authored
    harness/class choice and must not be shadowed by generated markdown.
    """
    routes: dict[tuple[str, str], str] = {}
    if not profiles_root.is_dir():
        return routes
    for path in sorted(profiles_root.glob("*/profile.md")):
        try:
            parsed = parse_profile(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not parsed.is_valid:
            continue
        harness = str(parsed.config.get("harness") or "").strip()
        default_class = str(parsed.config.get("default_class") or "").strip()
        if harness and default_class:
            routes.setdefault((harness, default_class), parsed.frontmatter.id or path.parent.name)
    return routes


def _write_activation(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".profile-activation-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


__all__ = [
    "ACTIVATION_FILENAME", "ACTIVATION_SCHEMA_VERSION", "CatalogProfile", "ProfileActivation",
    "active_catalog_profile_ids", "activation_path", "evaluate_catalog",
    "refresh_catalog_profiles", "shipped_profile_catalog",
]
