# tests/test_config_supervisor.py
"""Tests for SupervisorConfig.

The reflection and observation config sections were deleted with the
in-process Supervisor (llm-direct-path L6); only ``global`` remains.
"""


def test_supervisor_config_in_app_config(tmp_path):
    from src.config import AppConfig, SupervisorConfig

    app = AppConfig(data_dir=str(tmp_path / "data"))
    assert hasattr(app, "supervisor")
    assert isinstance(app.supervisor, SupervisorConfig)


def test_supervisor_config_has_no_reflection_or_observation():
    """Both sections retired with the in-process Supervisor."""
    from src.config import SupervisorConfig

    cfg = SupervisorConfig()
    assert not hasattr(cfg, "reflection")
    assert not hasattr(cfg, "observation")


def test_supervisor_global_idle_timeout_default():
    from src.config import GlobalSupervisorConfig, SupervisorConfig

    cfg = SupervisorConfig()
    assert isinstance(cfg.global_, GlobalSupervisorConfig)
    # 45 min — see dashboard-shell-v2 design §4.
    assert cfg.global_.idle_timeout_seconds == 2700


def test_supervisor_global_idle_timeout_from_yaml(tmp_path):
    import yaml

    from src.config import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(tmp_path / "data"),
                "database_path": str(tmp_path / "test.db"),
                "discord": {
                    "bot_token": "test-token-for-validation",
                    "guild_id": "123456789",
                },
                "supervisor": {"global": {"idle_timeout_seconds": 1800}},
            }
        )
    )
    cfg = load_config(str(cfg_path))
    assert cfg.supervisor.global_.idle_timeout_seconds == 1800


def test_retired_supervisor_sections_still_load(tmp_path):
    """An old config file with ``reflection``/``observation`` must not break."""
    import yaml

    from src.config import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(tmp_path / "data"),
                "database_path": str(tmp_path / "test.db"),
                "discord": {
                    "bot_token": "test-token-for-validation",
                    "guild_id": "123456789",
                },
                "supervisor": {
                    "reflection": {"level": "full"},
                    "observation": {"enabled": True},
                    "global": {"idle_timeout_seconds": 1800},
                },
            }
        )
    )
    cfg = load_config(str(cfg_path))
    assert cfg.supervisor.global_.idle_timeout_seconds == 1800


def test_supervisor_config_validation():
    from src.config import SupervisorConfig

    cfg = SupervisorConfig()
    errors = cfg.validate()
    assert len(errors) == 0


def test_check_deprecations_returns_empty(tmp_path):
    """check_deprecations returns empty list when no deprecated config is present."""
    from src.config import AppConfig

    app = AppConfig(data_dir=str(tmp_path / "data"))
    warnings = app.check_deprecations()
    assert len(warnings) == 0
