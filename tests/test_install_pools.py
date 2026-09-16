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


def test_every_active_worker_profile_becomes_a_pool(tmp_path):
    """Routing may send a task to any active rung; each needs a pool to run on."""
    claude = _rung(tmp_path, "claude", "standard-high")
    claude_deep = _rung(tmp_path, "claude", "deep-high")
    gemini = _rung(tmp_path, "gemini", "fast-low")

    result = ensure_default_pools(
        tmp_path, [_active(claude), _active(claude_deep), _active(gemini)], TUNED
    )

    assert result.created == ("deep-high-claude", "fast-low-gemini", "standard-high-claude")
    assert result.max_active == 4 and result.problem is None
    for profile_id in result.created:
        config = _config(tmp_path, profile_id)
        assert config["lifecycle"] == "pool"
        assert (config["min_active"], config["max_active"]) == (0, 4)
    # The class survives the rewrite.
    assert _config(tmp_path, "deep-high-claude")["default_class"] == "deep-high"


def test_a_cli_that_is_not_signed_in_gets_no_pool(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")
    codex = _rung(tmp_path, "codex", "standard-high")

    result = ensure_default_pools(tmp_path, [_active(claude), _active(codex, False)], TUNED)

    assert result.created == ("standard-high-claude",)
    assert _config(tmp_path, "standard-high-codex")["lifecycle"] == "task"


def test_an_install_with_one_pool_gets_the_rest(tmp_path):
    """The Mac that got only standard-high-claude from the first version."""
    standard = _rung(tmp_path, "claude", "standard-high")
    deep = _rung(tmp_path, "claude", "deep-high")
    ensure_default_pools(tmp_path, [_active(standard)], TUNED)

    result = ensure_default_pools(tmp_path, [_active(standard), _active(deep)], TUNED)

    assert result.created == ("deep-high-claude",)
    assert result.existing == ("standard-high-claude",)
    assert set(result.pools) == {"standard-high-claude", "deep-high-claude"}


def test_a_pool_the_operator_turned_back_to_task_stays_that_way(tmp_path):
    claude = _rung(tmp_path, "claude", "standard-high")
    ensure_default_pools(tmp_path, [_active(claude)], TUNED)
    path = tmp_path / "vault" / "agent-types" / "standard-high-claude" / "profile.md"
    from src.profiles.parser import update_config_keys

    path.write_text(
        update_config_keys(
            path.read_text(encoding="utf-8"),
            {"lifecycle": "task", "min_active": None, "max_active": None},
        ),
        encoding="utf-8",
    )

    result = ensure_default_pools(tmp_path, [_active(claude)], TUNED)

    assert result.created == ()
    assert _config(tmp_path, "standard-high-claude")["lifecycle"] == "task"


def test_a_pool_that_predates_the_record_is_remembered_too(tmp_path):
    """Pools made before pool-defaults.json existed must not be re-pooled later."""
    claude = _rung(tmp_path, "claude", "standard-high")
    path = tmp_path / "vault" / "agent-types" / "standard-high-claude" / "profile.md"
    from src.profiles.parser import update_config_keys

    path.write_text(
        update_config_keys(
            path.read_text(encoding="utf-8"),
            {"lifecycle": "pool", "min_active": 0, "max_active": 4},
        ),
        encoding="utf-8",
    )
    ensure_default_pools(tmp_path, [_active(claude)], TUNED)  # sees it, records it
    path.write_text(
        update_config_keys(
            path.read_text(encoding="utf-8"),
            {"lifecycle": "task", "min_active": None, "max_active": None},
        ),
        encoding="utf-8",
    )

    ensure_default_pools(tmp_path, [_active(claude)], TUNED)

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
