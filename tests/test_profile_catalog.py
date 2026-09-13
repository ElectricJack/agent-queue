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


def test_catalog_is_one_worker_per_harness_with_a_provider_appropriate_class():
    """One worker per harness — the *level* of a run comes from the task."""
    catalog = {profile.id: profile for profile in shipped_profile_catalog()}
    assert set(catalog) == {"worker-claude", "worker-codex"}
    assert catalog["worker-claude"].harness == "claude"
    assert catalog["worker-claude"].intelligence_class == "standard-high"
    # Astra is OpenAI-only, so the Codex worker is the one that can reach it.
    assert catalog["worker-codex"].harness == "codex"
    assert catalog["worker-codex"].intelligence_class == "astra-high"


def test_unready_provider_is_not_activated_and_carries_setup_guidance():
    activations = {row.profile.id: row for row in evaluate_catalog(_probes("codex"))}
    assert activations["worker-codex"].active
    claude = activations["worker-claude"]
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
        row for row in evaluate_catalog(probes) if row.profile.id == "worker-claude"
    )
    assert not activation.active
    assert "claude auth login" in (activation.remediation or "")


def test_refresh_seeds_ready_providers_and_preserves_existing_profile_edits(tmp_path):
    first = refresh_catalog_profiles(tmp_path, _probes("codex"))
    assert set(first["created"]) == {"worker-codex"}
    profile_path = tmp_path / "vault" / "agent-types" / "worker-codex" / "profile.md"
    parsed = parse_profile(profile_path.read_text(encoding="utf-8"))
    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.id == "worker-codex"
    assert parsed.config["harness"] == "codex"
    assert parsed.config["default_class"] == "astra-high"

    profile_path.write_text("# operator edit\n", encoding="utf-8")
    second = refresh_catalog_profiles(tmp_path, _probes("codex"))
    assert "worker-codex" in second["skipped"]
    assert profile_path.read_text(encoding="utf-8") == "# operator edit\n"


def test_refresh_after_provider_removal_deactivates_without_deleting_profiles(tmp_path):
    refresh_catalog_profiles(tmp_path, _probes("codex"))
    profile_path = tmp_path / "vault" / "agent-types" / "worker-codex" / "profile.md"
    refresh_catalog_profiles(tmp_path, _probes())

    assert profile_path.exists()
    assert active_catalog_profile_ids(tmp_path) == set()
    payload = json.loads(activation_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["profiles"]["worker-codex"]["active"] is False


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
    profiles = ["worker-claude", "worker-codex", "reviewer"]
    assert select_default_profile_id(profiles, eligible_profile_ids={"worker-codex"}) == (
        "worker-codex"
    )
    assert select_default_profile_id(profiles, eligible_profile_ids=set()) is None


def test_default_selector_never_chooses_a_disabled_profile():
    class Profile:
        id = "worker-codex"
        enabled = False

    assert select_default_profile_id([Profile(), "reviewer"]) == "reviewer"
