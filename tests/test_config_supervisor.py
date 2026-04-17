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
    assert cfg.enabled is True
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
    assert cfg.observation.enabled is True


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
