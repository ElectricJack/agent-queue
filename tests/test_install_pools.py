"""Default worker pools: a fresh install's first tasks need something to claim them.

The rung files here are the real ones `render_rung_stub` writes, and the
result is read back through the real profile parser, so a pool config the
daemon would reject cannot pass.
"""

from __future__ import annotations

from pathlib import Path

from src.install.pools import (
    FALLBACK_MAX_ACTIVE,
    configured_pools,
    ensure_default_pools,
)
from src.profiles.catalog import CatalogProfile, ProfileActivation, render_rung_stub
from src.profiles.parser import parse_profile

TUNED = {"swarm": {"enabled": True}, "resources": {"max_concurrent_agents": 4}}


def _rung(data_dir: Path, harness: str, intelligence_class: str) -> CatalogProfile:
    profile = CatalogProfile(
        id=f"{intelligence_class}-{harness}",
        provider_id={"claude": "anthropic", "codex": "openai", "gemini": "google"}[harness],
        harness=harness,
        intelligence_class=intelligence_class,
        name=f"{intelligence_class} {harness}",
        source_profile_id=f"worker-{harness}",
    )
    path = data_dir / "vault" / "agent-types" / profile.id / "profile.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_rung_stub(profile), encoding="utf-8")
    return profile


def _active(profile: CatalogProfile, active: bool = True) -> ProfileActivation:
    return ProfileActivation(profile=profile, active=active, reason="ok")


def _config(data_dir: Path, profile_id: str) -> dict:
    path = data_dir / "vault" / "agent-types" / profile_id / "profile.md"
    parsed = parse_profile(path.read_text(encoding="utf-8"))
    # The daemon's profile sync rejects what the parser reports as an error
    # (a sizing key on a task profile, a zero max, ...); a pool it would drop
    # is no pool at all.
    assert parsed.errors == [], parsed.errors
    return parsed.config


def test_each_signed_in_cli_gets_a_standard_high_pool(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")
    codex = _rung(tmp_path, "codex", "standard-high")
    deep = _rung(tmp_path, "claude", "deep-high")

    result = ensure_default_pools(
        tmp_path, [_active(claude), _active(codex), _active(deep)], TUNED
    )

    assert result.created == ("standard-high-claude", "standard-high-codex")
    assert result.max_active == 4 and result.problem is None
    config = _config(tmp_path, "standard-high-claude")
    assert config["lifecycle"] == "pool"
    assert (config["min_active"], config["max_active"]) == (0, 4)
    # The class survives the rewrite, and deeper classes are not pooled.
    assert config["default_class"] == "standard-high"
    assert _config(tmp_path, "deep-high-claude")["lifecycle"] == "task"


def test_a_cli_that_is_not_signed_in_gets_no_pool(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")
    gemini = _rung(tmp_path, "gemini", "standard-high")

    result = ensure_default_pools(tmp_path, [_active(claude), _active(gemini, False)], TUNED)

    assert result.created == ("standard-high-claude",)
    assert _config(tmp_path, "standard-high-gemini")["lifecycle"] == "task"


def test_an_install_that_already_has_a_pool_is_left_as_the_operator_made_it(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")
    codex = _rung(tmp_path, "codex", "standard-high")
    ensure_default_pools(tmp_path, [_active(codex)], TUNED)  # e.g. the operator's own pool

    result = ensure_default_pools(tmp_path, [_active(claude), _active(codex)], TUNED)

    assert result.created == ()
    assert result.existing == ("standard-high-codex",)
    assert _config(tmp_path, "standard-high-claude")["lifecycle"] == "task"


def test_a_rerun_does_not_rewrite_the_pools_it_made(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")
    ensure_default_pools(tmp_path, [_active(claude)], TUNED)
    path = tmp_path / "vault" / "agent-types" / "standard-high-claude" / "profile.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"max_active": 4', '"max_active": 9'),
        encoding="utf-8",
    )

    result = ensure_default_pools(tmp_path, [_active(claude)], TUNED)

    assert result.existing == ("standard-high-claude",)
    assert _config(tmp_path, "standard-high-claude")["max_active"] == 9


def test_pools_are_not_created_while_swarm_is_disabled(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")

    result = ensure_default_pools(tmp_path, [_active(claude)], {"swarm": {"enabled": False}})

    assert result.pools == ()
    assert "swarm.enabled" in (result.problem or "")
    assert configured_pools(tmp_path) == ()


def test_no_signed_in_cli_is_reported_not_silently_ignored(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")

    result = ensure_default_pools(tmp_path, [_active(claude, False)], TUNED)

    assert result.pools == ()
    assert "sign in" in (result.problem or "")


def test_an_untuned_configuration_gets_a_small_ceiling(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")

    result = ensure_default_pools(tmp_path, [_active(claude)], {"swarm": {"enabled": True}})

    assert result.max_active == FALLBACK_MAX_ACTIVE
    assert _config(tmp_path, "standard-high-claude")["max_active"] == FALLBACK_MAX_ACTIVE
