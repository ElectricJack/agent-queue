from pathlib import Path

from src.profiles.model_pin_migration import migrate_vault_profile_model_pins
from src.profiles.parser import parse_profile


def _write_class(root: Path) -> None:
    path = root / "intelligence-classes" / "standard-medium.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nid: standard-medium\nname: Standard\n---\n\n```json\n"
        '{"codex": {"model": "gpt-5.6-sol", "reasoning_effort": "medium"}}\n```\n'
    )


def _write_profile(root: Path, model: str) -> Path:
    path = root / "agent-types" / "worker" / "profile.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nid: worker\nname: Worker\n---\n\n## Config\n```json\n"
        f'{{"harness": "codex", "default_class": "standard-medium", "model": "{model}"}}\n'
        "```\n"
    )
    return path


def test_profile_parser_rejects_retired_model_key():
    parsed = parse_profile('## Config\n```json\n{"model": "old-model"}\n```\n')

    assert not parsed.is_valid
    assert "Config 'model' was removed" in parsed.errors[0]


def test_migration_drops_matching_model_pin(tmp_path):
    vault = tmp_path / "vault"
    _write_class(vault)
    profile = _write_profile(vault, "gpt-5.6-sol")

    result = migrate_vault_profile_model_pins(vault)

    assert result[0].matched is True
    assert '"model"' not in profile.read_text()
    assert parse_profile(profile.read_text()).is_valid


def test_migration_drops_mismatched_model_pin_and_logs_it(tmp_path, caplog):
    vault = tmp_path / "vault"
    _write_class(vault)
    profile = _write_profile(vault, "old-model")

    result = migrate_vault_profile_model_pins(vault)

    assert result[0].matched is False
    assert '"model"' not in profile.read_text()
    assert "Dropped legacy profile model pin" in caplog.text
