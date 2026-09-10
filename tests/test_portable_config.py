"""Safety and round-trip coverage for portable AQ configuration bundles."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import yaml

from src.portable_config import (
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    CONFIG_NAME,
    MANIFEST_NAME,
    PortableBundleError,
    export_bundle,
    import_bundle,
    inspect_bundle,
)
from src.profiles.parser import agent_profile_to_markdown


class FakeProfileDB:
    def __init__(self):
        self.profiles = {}

    async def get_profile(self, profile_id):
        return self.profiles.get(profile_id)

    async def upsert_profile(self, profile):
        self.profiles[profile.id] = profile
        return "created"


def _write_source(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "source-data"
    config = tmp_path / "source.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "resources": {"max_concurrent_agents": 3, "test_slots": 1},
                "swarm": {"enabled": True, "global_max_active": 3},
                "scheduling": {"rolling_window_hours": 12},
                "database": {"url": "postgresql+asyncpg://secret@localhost/aq"},
                "project_roots": [{"id": "personal", "path": "/home/me/code"}],
                "memory": {"enabled": True},
                "discord": {"bot_token": "do-not-export"},
            }
        )
    )
    profile_path = data_dir / "vault" / "agent-types" / "reviewer" / "profile.md"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        agent_profile_to_markdown(
            id="reviewer",
            name="Reviewer",
            harness="codex",
            mcp_servers=["lint", "docs"],
            allowed_tools=["Read", "Grep"],
        )
    )
    # This must never be swept in just because it shares the vault root.
    (data_dir / "vault" / "projects" / "p" / "memory").mkdir(parents=True)
    (data_dir / "vault" / "projects" / "p" / "memory" / "secret.md").write_text("private")
    return config, data_dir


def test_export_preview_excludes_project_data_credentials_and_paths(tmp_path):
    config, data_dir = _write_source(tmp_path)
    destination = tmp_path / "portable.aqbundle"

    receipt = export_bundle(str(destination), str(config), str(data_dir))
    inspected = inspect_bundle(str(destination))

    assert receipt["profiles"] == ["reviewer"]
    assert set(inspected["config"]) == {"resources", "scheduling", "swarm"}
    text = destination.read_bytes()
    assert b"do-not-export" not in text
    assert b"postgresql+asyncpg" not in text
    assert b"/home/me/code" not in text
    assert b"private" not in text


@pytest.mark.asyncio
async def test_import_roundtrip_preserves_profile_mcp_relationships(tmp_path):
    source_config, source_data = _write_source(tmp_path)
    destination = tmp_path / "portable.aqbundle"
    export_bundle(str(destination), str(source_config), str(source_data))

    target_config = tmp_path / "target.yaml"
    target_config.write_text(
        yaml.safe_dump(
            {"database": {"url": "postgresql+asyncpg://localhost/aq"}, "messaging_platform": "none"}
        )
    )
    target_data = tmp_path / "target-data"
    db = FakeProfileDB()

    result = await import_bundle(
        str(destination),
        config_path=str(target_config),
        data_dir=str(target_data),
        db=db,
    )

    assert result["applied"] is True
    assert yaml.safe_load(target_config.read_text())["resources"]["max_concurrent_agents"] == 3
    assert db.profiles["reviewer"].mcp_servers == ["lint", "docs"]
    assert (target_data / "vault" / "agent-types" / "reviewer" / "profile.md").is_file()


@pytest.mark.asyncio
async def test_import_default_conflict_policy_keeps_existing_customization(tmp_path):
    source_config, source_data = _write_source(tmp_path)
    destination = tmp_path / "portable.aqbundle"
    export_bundle(str(destination), str(source_config), str(source_data))
    target_config = tmp_path / "target.yaml"
    target_config.write_text(
        yaml.safe_dump(
            {
                "resources": {"max_concurrent_agents": 9},
                "database": {"url": "postgresql+asyncpg://localhost/aq"},
                "messaging_platform": "none",
            }
        )
    )
    target_data = tmp_path / "target-data"
    existing = target_data / "vault" / "agent-types" / "reviewer" / "profile.md"
    existing.parent.mkdir(parents=True)
    existing.write_text(agent_profile_to_markdown(id="reviewer", name="Customized reviewer"))

    result = await import_bundle(
        str(destination), config_path=str(target_config), data_dir=str(target_data), db=FakeProfileDB()
    )

    assert result["skipped_config_sections"] == ["resources"]
    assert result["skipped_profiles"] == ["reviewer"]
    assert yaml.safe_load(target_config.read_text())["resources"]["max_concurrent_agents"] == 9
    assert "Customized reviewer" in existing.read_text()


@pytest.mark.asyncio
async def test_import_conflict_error_reports_collisions_without_writing(tmp_path):
    source_config, source_data = _write_source(tmp_path)
    destination = tmp_path / "portable.aqbundle"
    export_bundle(str(destination), str(source_config), str(source_data))
    target_config = tmp_path / "target.yaml"
    target_config.write_text(
        yaml.safe_dump(
            {"resources": {"max_concurrent_agents": 9}, "messaging_platform": "none"}
        )
    )
    target_data = tmp_path / "target-data"
    existing = target_data / "vault" / "agent-types" / "reviewer" / "profile.md"
    existing.parent.mkdir(parents=True)
    existing.write_text(agent_profile_to_markdown(id="reviewer", name="Customized reviewer"))
    before = target_config.read_text()

    result = await import_bundle(
        str(destination),
        config_path=str(target_config),
        data_dir=str(target_data),
        db=FakeProfileDB(),
        conflict="error",
    )

    assert result["error"] == "bundle conflicts with existing customization"
    assert result["conflicts"] == {"config": ["resources"], "profiles": ["reviewer"]}
    assert target_config.read_text() == before


def test_import_rejects_path_traversal_and_unmanifested_secret(tmp_path):
    bundle = tmp_path / "unsafe.aqbundle"
    manifest = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "config": CONFIG_NAME,
        "profiles": [],
    }
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(MANIFEST_NAME, json.dumps(manifest))
        archive.writestr(CONFIG_NAME, "resources:\n  test_slots: 1\n")
        archive.writestr("../project-memory.txt", "must not be accepted")

    with pytest.raises(PortableBundleError, match="unsafe bundle member path"):
        inspect_bundle(str(bundle))


def test_export_refuses_credentials_even_inside_an_allowlisted_section(tmp_path):
    config, data_dir = _write_source(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["resources"]["api_key"] = "sk-this-must-never-enter-a-bundle"
    config.write_text(yaml.safe_dump(raw))

    with pytest.raises(PortableBundleError, match="secret-like key"):
        export_bundle(str(tmp_path / "unsafe.aqbundle"), str(config), str(data_dir))
