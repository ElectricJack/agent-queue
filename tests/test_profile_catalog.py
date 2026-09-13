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


def _seed_vault(tmp_path):
    """A vault with the shipped classes and the shipped worker templates."""
    from src.vault import ensure_default_intelligence_classes, ensure_default_profiles

    ensure_default_intelligence_classes(str(tmp_path))
    ensure_default_profiles(str(tmp_path))


def test_the_worker_set_is_the_class_x_harness_cross_product():
    """Nothing here enumerates workers: the class files decide what exists."""
    catalog = {profile.id: profile for profile in shipped_profile_catalog()}

    assert set(catalog) == {
        "fast-low-claude", "fast-high-claude", "standard-high-claude",
        "deep-low-claude", "deep-high-claude",
        "fast-low-codex", "fast-high-codex", "standard-high-codex",
        "deep-low-codex", "deep-high-codex", "astra-low-codex", "astra-high-codex",
    }
    rung = catalog["deep-high-claude"]
    assert (rung.harness, rung.intelligence_class) == ("claude", "deep-high")
    assert rung.source_profile_id == "worker-claude"


def test_a_class_with_no_slice_for_a_harness_yields_no_rung():
    """The two provider rules are properties of the class files, not code."""
    ids = {profile.id for profile in shipped_profile_catalog()}

    # Astra is OpenAI-only: no anthropic slice, so no Claude rung.
    assert "astra-high-claude" not in ids
    assert "astra-high-codex" in ids
    # Nothing from the deep tier upward has a google slice, and Gemini ships
    # no template at all, so it derives nothing.
    assert not any(id_.endswith("-gemini") for id_ in ids)


def test_an_operators_own_class_yields_rungs_too(tmp_path):
    _seed_vault(tmp_path)
    (tmp_path / "vault" / "intelligence-classes" / "house-style.md").write_text(
        "---\nid: house-style\nname: House Style\n---\n\n"
        '```json\n{"anthropic": {"model": "claude-opus-5", "thinking": "high"}}\n```\n',
        encoding="utf-8",
    )

    created = set(refresh_catalog_profiles(tmp_path, _probes("claude"))["created"])

    assert "house-style-claude" in created
    # It has no openai slice, so it yields no Codex rung.
    assert "house-style-codex" not in created


def test_a_rung_is_a_stub_that_inherits_its_template(tmp_path):
    _seed_vault(tmp_path)
    refresh_catalog_profiles(tmp_path, _probes("claude"))
    path = tmp_path / "vault" / "agent-types" / "deep-high-claude" / "profile.md"

    parsed = parse_profile(path.read_text(encoding="utf-8"))

    assert parsed.is_valid, parsed.errors
    assert parsed.frontmatter.extends == "worker-claude"
    # Its own state, and nothing else: no copied prompt to drift.
    assert parsed.config == {"default_class": "deep-high", "lifecycle": "task"}
    assert parsed.role == ""
    assert parsed.capabilities is None


def test_a_template_is_not_itself_a_profile(tmp_path):
    _seed_vault(tmp_path)
    template = tmp_path / "vault" / "agent-types" / "worker-claude" / "profile.md"

    parsed = parse_profile(template.read_text(encoding="utf-8"))

    assert parsed.frontmatter.template is True
    assert parsed.frontmatter.id not in {p.id for p in shipped_profile_catalog()}


def test_unready_provider_is_not_activated_and_carries_setup_guidance():
    activations = {row.profile.id: row for row in evaluate_catalog(_probes("codex"))}
    assert activations["standard-high-codex"].active
    claude = activations["standard-high-claude"]
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
        row for row in evaluate_catalog(probes) if row.profile.id == "deep-high-claude"
    )
    assert not activation.active
    assert "claude auth login" in (activation.remediation or "")


def test_refresh_seeds_ready_providers_and_preserves_existing_profile_edits(tmp_path):
    _seed_vault(tmp_path)
    first = refresh_catalog_profiles(tmp_path, _probes("codex"))
    assert set(first["created"]) == {
        "fast-low-codex", "fast-high-codex", "standard-high-codex",
        "deep-low-codex", "deep-high-codex", "astra-low-codex", "astra-high-codex",
    }
    assert not any(id_.endswith("-claude") for id_ in first["created"])
    profile_path = tmp_path / "vault" / "agent-types" / "standard-high-codex" / "profile.md"

    profile_path.write_text("# operator edit\n", encoding="utf-8")
    second = refresh_catalog_profiles(tmp_path, _probes("codex"))
    assert "standard-high-codex" in second["skipped"]
    assert profile_path.read_text(encoding="utf-8") == "# operator edit\n"


def test_refresh_after_provider_removal_deactivates_without_deleting_profiles(tmp_path):
    _seed_vault(tmp_path)
    refresh_catalog_profiles(tmp_path, _probes("codex"))
    profile_path = tmp_path / "vault" / "agent-types" / "deep-high-codex" / "profile.md"
    refresh_catalog_profiles(tmp_path, _probes())

    assert profile_path.exists()
    assert active_catalog_profile_ids(tmp_path) == set()
    payload = json.loads(activation_path(tmp_path).read_text(encoding="utf-8"))
    assert payload["profiles"]["deep-high-codex"]["active"] is False


def test_an_activation_record_from_an_older_worker_set_is_not_authoritative(tmp_path):
    """A stale record must read as 'unknown', never as 'nothing is eligible'."""
    activation_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    activation_path(tmp_path).write_text(
        json.dumps({
            "schema_version": 1,
            "profiles": {"worker-standard-medium-claude": {"active": True}},
        }),
        encoding="utf-8",
    )

    assert active_catalog_profile_ids(tmp_path) is None


def test_refresh_skips_every_retired_catalog_id(tmp_path):
    _seed_vault(tmp_path)
    for profile in shipped_profile_catalog():
        assert retire_default(str(tmp_path), profile.id)

    refreshed = refresh_catalog_profiles(tmp_path, _probes("codex"))

    assert refreshed["created"] == []
    assert set(refreshed["retired"]) == {
        "fast-low-codex", "fast-high-codex", "standard-high-codex",
        "deep-low-codex", "deep-high-codex", "astra-low-codex", "astra-high-codex",
    }


def test_refresh_does_not_shadow_an_existing_harness_class_route(tmp_path):
    _seed_vault(tmp_path)
    existing = tmp_path / "vault" / "agent-types" / "house-codex" / "profile.md"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        """---
id: house-codex
name: House Codex
---

## Config
```json
{"harness": "codex", "default_class": "astra-high", "lifecycle": "pool"}
```
""",
        encoding="utf-8",
    )

    refreshed = refresh_catalog_profiles(tmp_path, _probes("codex"))

    assert "astra-high-codex" in refreshed["duplicates"]
    assert not (tmp_path / "vault" / "agent-types" / "astra-high-codex").exists()
    assert "astra-low-codex" in refreshed["created"]


def test_a_stage_profile_does_not_block_the_rung_of_its_class(tmp_path):
    """``triage`` runs fast-low on Claude; that is not a fast-low worker."""
    _seed_vault(tmp_path)

    created = set(refresh_catalog_profiles(tmp_path, _probes("claude"))["created"])

    assert {"fast-low-claude", "deep-high-claude"} <= created


def test_default_selector_excludes_catalog_profiles_not_in_activation_record():
    profiles = ["standard-high-claude", "standard-high-codex", "reviewer"]
    assert select_default_profile_id(
        profiles, eligible_profile_ids={"standard-high-codex"}
    ) == "standard-high-codex"
    assert select_default_profile_id(profiles, eligible_profile_ids=set()) is None


def test_default_selector_never_chooses_a_disabled_profile():
    class Profile:
        id = "worker-codex"
        enabled = False

    assert select_default_profile_id([Profile(), "reviewer"]) == "reviewer"
