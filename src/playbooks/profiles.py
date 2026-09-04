"""Capability lookup for profiles shipped with Playbooks V2 artifacts."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def shipped_profile_lookup(root: str | None = None) -> Any:
    from src.playbooks.validation import VaultProfileLookup
    from src.profiles.drift import defaults_root, shipped_profile_path, system_profile_ids
    from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile

    base = root or defaults_root()
    profiles: dict[str, Any] = {}
    for profile_id in system_profile_ids(base):
        path = shipped_profile_path(profile_id, base)
        parsed = parse_profile(Path(path).read_text(encoding="utf-8"))
        if not parsed.is_valid:
            raise ValueError(f"shipped profile {path} does not parse: {parsed.errors}")
        fields = parsed_profile_to_agent_profile(parsed)
        profiles[fields["id"]] = SimpleNamespace(**fields)
    return VaultProfileLookup(profiles)


def profile_fingerprints_for(profile_lookup: Any, profile_ids: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for profile_id in profile_ids:
        policy = profile_lookup.policy(profile_id)
        if policy is not None:
            result[profile_id] = str(policy.fingerprint())
    return result


def shipped_profile_fingerprints(root: str | None = None) -> dict[str, str]:
    from src.profiles.drift import defaults_root, system_profile_ids

    base = root or defaults_root()
    return profile_fingerprints_for(shipped_profile_lookup(base), system_profile_ids(base))
