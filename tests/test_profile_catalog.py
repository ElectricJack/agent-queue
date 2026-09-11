"""Provider readiness controls generated worker-profile eligibility."""

from __future__ import annotations

import json

from src.install.logins import AuthProbe
from src.profiles.catalog import (
    active_catalog_profile_ids,
    activation_path,
    evaluate_catalog,
    refresh_catalog_profiles,
    shipped_profile_catalog,
)
from src.profiles.default_selection import select_default_profile_id
from src.profiles.parser import parse_profile
from src.profiles.retired_defaults import retire_default


def _probes(*ready: str) -> tuple[AuthProbe, ...]:
    return tuple(
        AuthProbe(provider, installed=provider in ready, authenticated=provider in ready)
        for provider in ("claude", "codex", "gemini")
    )


def test_catalog_contains_every_provider_explicit_worker_tier():
    ids = {profile.id for profile in shipped_profile_catalog()}
    assert ids == {
        f"worker-{tier}-{provider}"
        for tier in ("fast-medium", "standard-medium", "deep-high")
        for provider in ("claude", "codex", "gemini")
    }


def test_unready_provider_is_not_activated_and_carries_setup_guidance():
    activations = {row.profile.id: row for row in evaluate_catalog(_probes("codex"))}
    assert activations["worker-standard-medium-codex"].active
    claude = activations["worker-standard-medium-claude"]
    assert not claude.active
    assert "not installed" in claude.reason
    assert "rerun `aq install`" in (claude.remediation or "")


def test_installed_but_unauthenticated_provider_has_login_remediation():
    probes = (
        AuthProbe("claude", installed=True, authenticated=False),
        AuthProbe("codex", installed=False, authenticated=False),
        AuthProbe("gemini", installed=False, authenticated=False),
    )
    activation = next(
        row for row in evaluate_catalog(probes) if row.profile.id == "worker-deep-high-claude"
    )
    assert not activation.active
    assert "claude auth login" in (activation.remediation or "")


def test_refresh_seeds_ready_siblings_and_preserves_existing_profile_edits(tmp_path):
    first = refresh_catalog_profiles(tmp_path, _probes("codex"))
    assert set(first["created"]) == {
        "worker-fast-medium-codex",
        "worker-standard-medium-codex",
        "worker-deep-high-codex",
    }
    profile_path = tmp_path / "vault" / "agent-types" / "worker-standard-medium-codex" / "profile.md"
    parsed = parse_profile(profile_path.read_text(encoding="utf-8"))
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "worker-standard-medium-codex"
    assert parsed.config["harness"] == "codex"

    profile_path.write_text("# operator edit\n", encoding="utf-8")
    second = refresh_catalog_profiles(tmp_path, _probes("codex"))
    assert "worker-standard-medium-codex" in second["skipped"]
    assert profile_path.read_text(encoding="utf-8") == "# operator edit\n"


def test_refresh_after_provider_removal_deactivates_without_deleting_profiles(tmp_path):
    refresh_catalog_profiles(tmp_path, _probes("codex"))
    profile_path = tmp_path / "vault" / "agent-types" / "worker-deep-high-codex" / "profile.md"
    refresh_catalog_profiles(tmp_path, _probes())

    assert profile_path.exists()
    assert active_catalog_profile_ids(tmp_path) == set()
    payload = json.loads(activation_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["profiles"]["worker-deep-high-codex"]["active"] is False


def test_refresh_skips_every_retired_catalog_id(tmp_path):
    for profile in shipped_profile_catalog():
        assert retire_default(str(tmp_path), profile.id)

    refreshed = refresh_catalog_profiles(tmp_path, _probes("codex"))

    assert refreshed["created"] == []
    assert set(refreshed["retired"]) == {
        "worker-fast-medium-codex",
        "worker-standard-medium-codex",
        "worker-deep-high-codex",
    }


def test_refresh_does_not_shadow_an_existing_harness_class_route(tmp_path):
    existing = tmp_path / "vault" / "agent-types" / "standard-medium-codex" / "profile.md"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        """---
id: standard-medium-codex
name: Standard Codex
---

## Config
```json
{"harness": "codex", "default_class": "standard-medium", "lifecycle": "pool"}
```
""",
        encoding="utf-8",
    )

    refreshed = refresh_catalog_profiles(tmp_path, _probes("codex"))

    assert "worker-standard-medium-codex" in refreshed["duplicates"]
    assert not (tmp_path / "vault" / "agent-types" / "worker-standard-medium-codex").exists()
    assert set(refreshed["created"]) == {"worker-fast-medium-codex", "worker-deep-high-codex"}


def test_materialized_non_claude_profile_describes_its_own_harness(tmp_path):
    refresh_catalog_profiles(tmp_path, _probes("codex"))
    text = (tmp_path / "vault" / "agent-types" / "worker-standard-medium-codex" / "profile.md").read_text()
    assert "concrete Codex model" in text
    assert "A Codex or Gemini equivalent" not in text


def test_default_selector_excludes_catalog_profiles_not_in_activation_record():
    profiles = [
        "worker-standard-medium-claude",
        "worker-standard-medium-codex",
        "reviewer",
    ]
    assert select_default_profile_id(profiles, eligible_profile_ids={"worker-standard-medium-codex"}) == (
        "worker-standard-medium-codex"
    )
    assert select_default_profile_id(profiles, eligible_profile_ids=set()) is None


def test_default_selector_never_chooses_a_disabled_profile():
    class Profile:
        id = "worker-standard-medium-codex"
        enabled = False

    assert select_default_profile_id([Profile(), "reviewer"]) == "reviewer"
