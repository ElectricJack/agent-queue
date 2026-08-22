# tests/test_config_supervisor.py
"""Tests for SupervisorConfig and ReflectionConfig."""


def test_reflection_config_defaults():
    from src.config import ReflectionConfig

    cfg = ReflectionConfig()
    assert cfg.level == "full"
    assert cfg.periodic_interval == 900
    assert cfg.max_depth == 3
    assert cfg.per_cycle_token_cap == 10000
    assert cfg.hourly_token_circuit_breaker == 100000


def test_reflection_config_validation_valid():
    from src.config import ReflectionConfig

    cfg = ReflectionConfig(level="moderate", max_depth=2)
    errors = cfg.validate()
    assert len(errors) == 0


def test_reflection_config_validation_invalid_level():
    from src.config import ReflectionConfig

    cfg = ReflectionConfig(level="turbo")
    errors = cfg.validate()
    assert any("level" in str(e) for e in errors)


def test_reflection_config_validation_invalid_depth():
    from src.config import ReflectionConfig

    cfg = ReflectionConfig(max_depth=0)
    errors = cfg.validate()
    assert any("max_depth" in str(e) for e in errors)


def test_supervisor_config_defaults():
    from src.config import SupervisorConfig

    cfg = SupervisorConfig()
    assert cfg.reflection is not None
    assert cfg.reflection.level == "full"


def test_supervisor_config_in_app_config(tmp_path):
    from src.config import AppConfig, SupervisorConfig

    app = AppConfig(data_dir=str(tmp_path / "data"))
    assert hasattr(app, "supervisor")
    assert isinstance(app.supervisor, SupervisorConfig)


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


def test_reflection_config_off_disables():
    from src.config import ReflectionConfig

    cfg = ReflectionConfig(level="off")
    errors = cfg.validate()
    assert len(errors) == 0


def test_supervisor_config_validation():
    from src.config import SupervisorConfig

    cfg = SupervisorConfig()
    errors = cfg.validate()
    assert len(errors) == 0


def test_observation_config_defaults():
    from src.config import ObservationConfig

    cfg = ObservationConfig()
    # Paused by default during the framework overhaul — this is the real
    # chat-analyzer switch.  See docs/specs/design/feature-pauses.md §2.3.
    assert cfg.enabled is False
    assert cfg.batch_window_seconds == 60
    assert cfg.max_buffer_size == 20
    assert cfg.stage1_keywords == []


def test_observation_config_validation():
    from src.config import ObservationConfig

    cfg = ObservationConfig(batch_window_seconds=0)
    errors = cfg.validate()
    assert any("batch_window_seconds" in str(e) for e in errors)


def test_supervisor_config_has_observation():
    from src.config import SupervisorConfig

    cfg = SupervisorConfig()
    assert hasattr(cfg, "observation")
    # Paused by default — see docs/specs/design/feature-pauses.md §2.3.
    assert cfg.observation.enabled is False


def test_observation_config_from_yaml():
    from src.config import SupervisorConfig, ObservationConfig

    cfg = SupervisorConfig(
        observation=ObservationConfig(
            enabled=False,
            batch_window_seconds=30,
            max_buffer_size=10,
            stage1_keywords=["deploy", "hotfix"],
        )
    )
    assert cfg.observation.enabled is False
    assert cfg.observation.batch_window_seconds == 30
    assert cfg.observation.stage1_keywords == ["deploy", "hotfix"]


def test_check_deprecations_returns_empty(tmp_path):
    """check_deprecations returns empty list when no deprecated config is present."""
    from src.config import AppConfig

    app = AppConfig(data_dir=str(tmp_path / "data"))
    warnings = app.check_deprecations()
    assert len(warnings) == 0
