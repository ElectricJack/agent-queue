"""Safety and round-trip coverage for portable AQ configuration bundles."""

from __future__ import annotations

import json
import zipfile
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from src.portable_config import (
    BUNDLE_FORMAT,
    CONFIG_NAME,
    MANIFEST_NAME,
    PortableBundleError,
    PORTABLE_CONFIG_SECTIONS,
    bundle_preview,
    export_bundle,
    import_bundle,
    inspect_bundle,
    read_bundle,
)
from src.config import AppConfig
from src.models import Project, RepoConfig, RepoSourceType
from src.portable_setup import PoolSettings, Setup
from src.profiles.catalog import CatalogProfile, render_rung_stub
from src.profiles.parser import agent_profile_to_markdown, parse_profile, update_config_keys
from src.profiles.sync import sync_profile_text_to_db


class FakeProfileDB:
    def __init__(self):
        self.profiles = {}

    async def get_profile(self, profile_id):
        return self.profiles.get(profile_id)

    async def upsert_profile(self, profile):
        self.profiles[profile.id] = profile
        return "created"


CLASS_TEXT = '''---
id: portable
name: Portable
customized: true
---
```json
{"codex":{"model":"custom-model","reasoning_effort":"high"}}
```
'''
TEMPLATE_TEXT = '''---
id: z-worker-codex
name: Worker
template: true
---
## Config
```json
{"harness":"codex","lifecycle":"pool","min_active":2,"max_active":4}
```
## Role
Use the project's instructions.
'''


def put_text(data_dir, relative, text):
    path = Path(data_dir) / "vault" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


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
    assert set(inspected["config"]) == {"resources", "scheduling", "swarm", "memory"}
    text = destination.read_bytes()
    assert b"do-not-export" not in text
    assert b"postgresql+asyncpg" not in text
    assert b"/home/me/code" not in text
    assert b"private" not in text


def test_every_application_section_has_an_explicit_portability_decision():
    local = {"data_dir", "workspace_dir", "project_roots", "database_path", "git_identity"}
    sections = {entry.name for entry in fields(AppConfig) if not entry.name.startswith("_")}
    assert sections - local <= PORTABLE_CONFIG_SECTIONS


async def test_replacing_profile_preserves_destination_install_credentials_and_paths(tmp_path):
    source, data = _write_source(tmp_path)
    source_profile = data / "vault/agent-types/reviewer/profile.md"
    source_profile.write_text(agent_profile_to_markdown(
        id="reviewer", name="Incoming reviewer", harness="codex",
        install={"pip": ["source-package"], "env": {"TOKEN": "source-secret"},
                 "source_path": "/home/source/code"},
    ))
    archive = tmp_path / "profile.aqbundle"
    export_bundle(str(archive), str(source), str(data))
    assert "source-secret" not in read_bundle(str(archive)).profiles["reviewer"]
    target = tmp_path / "profile-target.yaml"
    target.write_text(yaml.safe_dump({"messaging_platform": "none", "database": {
        "url": "postgresql+asyncpg://localhost/aq",
    }}))
    target_data = tmp_path / "profile-target"
    profile = put_text(target_data, "agent-types/reviewer/profile.md", agent_profile_to_markdown(
        id="reviewer", name="Destination reviewer", harness="codex",
        install={"pip": ["old-package"], "env": {"TOKEN": "destination-secret"},
                 "source_path": "/home/destination/code"},
    ))
    result = await import_bundle(str(archive), config_path=str(target), data_dir=str(target_data),
                                 db=FakeProfileDB(), conflict="replace")
    assert result.get("error") is None, result
    parsed = parse_profile(profile.read_text())
    assert parsed.frontmatter.name == "Incoming reviewer"
    assert parsed.install == {"pip": ["source-package"], "env": {"TOKEN": "destination-secret"},
                              "source_path": "/home/destination/code"}


def test_version_one_bundle_remains_readable(tmp_path):
    archive = tmp_path / "old.aqbundle"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(MANIFEST_NAME, json.dumps({
            "format": BUNDLE_FORMAT, "version": 1, "config": CONFIG_NAME, "profiles": [],
        }))
        bundle.writestr(CONFIG_NAME, "sessions:\n  lease_ttl_seconds: 900\n")
    result = inspect_bundle(str(archive))
    assert result["version"] == 1
    assert result["config"]["sessions"]["lease_ttl_seconds"] == 900


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


async def test_command_export_import_recreates_clean_setup(command_handler_factory, tmp_path):
    handler = await command_handler_factory()
    try:
        source_data = handler.config.data_dir
        source = tmp_path / "full-source.yaml"
        source.write_text("messaging_platform: none\nsessions:\n  lease_ttl_seconds: 777\n")
        handler.config._config_path = str(source)
        put_text(source_data, "intelligence-classes/portable.md", CLASS_TEXT)
        put_text(source_data, "agent-types/z-worker-codex/profile.md", TEMPLATE_TEXT)
        rung = render_rung_stub(CatalogProfile(
            id="a-portable-codex", provider_id="codex", harness="codex",
            intelligence_class="portable", name="Portable worker", source_profile_id="z-worker-codex",
        ))
        rung = update_config_keys(rung, {"lifecycle": "pool", "min_active": 2, "max_active": 4})
        path = put_text(source_data, "agent-types/a-portable-codex/profile.md", rung)
        synced = await sync_profile_text_to_db(rung, handler.db, source_path=str(path))
        assert synced.success, synced.errors
        await handler.db.update_profile(
            "a-portable-codex", enabled=False, min_active=1, max_active=None,
            min_per_project=1, max_claims_per_session=7,
        )
        project = Project(
            id="recreated", name="Recreated project", credit_weight=3.0, max_concurrent_agents=4,
            repo_url="https://github.com/acme/recreated.git", repo_default_branch="develop",
            assignment_playbook_id="custom-router", preferred_provider="openai",
        )
        await handler.db.create_project(project)
        await handler.db.create_repo(RepoConfig(
            id="source-repo", project_id=project.id, source_type=RepoSourceType.CLONE,
            url=project.repo_url, source_path="/home/source/repo", checkout_base_path="/tmp/checkouts",
            default_branch="develop",
        ))
        put_text(source_data, "projects/recreated/overrides/worker.md", "Follow review policy.\n")
        put_text(source_data, "system/playbooks/custom-router.md", "# Custom routing policy\n")
        put_text(source_data, "mcp-servers/docs.md", "---\nname: docs\ntransport: stdio\ncommand: uvx\nenv:\n  TOKEN: source-secret\n---\n")
        put_text(source_data, "projects/recreated/workspace-kinds/docs.md", "---\nid: docs\nwritable: false\nis_git_repo: false\n---\nDocumentation workspace.\n")
        put_text(source_data, "projects/recreated/policy/ci-checks.json", '{"required_checks": ["portable-check"]}\n')
        put_text(source_data, "projects/recreated/memory/private.md", "personal history")
        archive = tmp_path / "full.aqbundle"
        exported = await handler.execute("export_portable_config", {"destination": str(archive)})
        assert exported["success"], exported
        bundle = read_bundle(str(archive))
        assert bundle.setup.pools["a-portable-codex"].enabled is False
        assert bundle.setup.pools["a-portable-codex"].max_active is None
        assert "a-portable-codex" not in bundle.profiles
        assert bundle.setup.projects[project.id].repos[0].url == project.repo_url
        assert "personal history" not in archive.read_bytes().decode("latin1")

        # The second installation starts with an empty configuration database.
        await handler.db.delete_project(project.id)
        await handler.db.delete_profile("a-portable-codex")
        target_data = tmp_path / "target-setup"
        target = tmp_path / "full-target.yaml"
        target.write_text(yaml.safe_dump({
            "messaging_platform": "none", "database": {"url": handler.config.database.url},
        }))
        handler.config.data_dir = str(target_data)
        handler.config._config_path = str(target)
        imported = await handler.execute("import_portable_config", {"source": str(archive)})
        assert imported["success"] and imported["applied"], imported
        restored = await handler.db.get_profile("a-portable-codex")
        assert (restored.enabled, restored.min_active, restored.max_active,
                restored.min_per_project, restored.max_claims_per_session) == (False, 1, None, 1, 7)
        assert restored.harness == "codex" and restored.default_class == "portable"
        restored_project = await handler.db.get_project(project.id)
        assert (restored_project.name, restored_project.credit_weight,
                restored_project.assignment_playbook_id, restored_project.repo_default_branch) == (
                    project.name, 3.0, "custom-router", "develop",
                )
        repo = await handler.db.get_repo("source-repo")
        assert repo.source_path == "" and repo.checkout_base_path == ""
        assert repo.url == project.repo_url and repo.default_branch == "develop"
        assert (target_data / "vault/intelligence-classes/portable.md").read_text() == bundle.intelligence_classes["portable"]
        assert handler.orchestrator.intelligence_classes["portable"].mapping["codex"]["model"] == "custom-model"
        assert (target_data / "vault/projects/recreated/overrides/worker.md").is_file()
        assert (target_data / "vault/system/playbooks/custom-router.md").is_file()
        assert (target_data / "vault/projects/recreated/workspace-kinds/docs.md").is_file()
        assert "source-secret" not in (target_data / "vault/mcp-servers/docs.md").read_text()
        assert json.loads((target_data / "vault/projects/recreated/policy/ci-checks.json").read_text())["required_checks"] == ["portable-check"]
        assert "vault policy registries" in imported["requires_restart"]
        assert not (target_data / "vault/projects/recreated/memory").exists()
        assert yaml.safe_load(target.read_text())["sessions"]["lease_ttl_seconds"] == 777
    finally:
        await handler.db.close()


async def test_replacement_merges_local_fields_in_every_config_section(tmp_path):
    source, data = _write_source(tmp_path)
    raw = yaml.safe_load(source.read_text())
    raw.update({
        "sessions": {"lease_ttl_seconds": 777, "tmux_socket": "/tmp/source-socket"},
        "messages": {"retention_days": 17},
        "supervisor": {"provider": "openai"},
        "worktrees": {"enabled": False},
        "test_selection": {"enabled": True},
        "reports": {"timezone": "America/Los_Angeles", "morning": {
            "enabled": True, "destination": "discord:111111111111111111",
        }},
        "knowledge": {"enabled": True},
        "dashboard_server": {"port": 9002, "public_url": "https://source.local"},
        "dashboard": {"graph_layout": {"enabled": False}, "public_url": "https://source.local"},
        "events": {"validation_mode": "warn"},
        "inbox": {"enabled": True, "projects": ["p"], "client_secret": "source-secret"},
        "planner": {"enabled": True, "max_iterations": 3},
        "llm": {"provider": "openai", "max_tokens": 8000, "api_key": "source-key"},
    })
    source.write_text(yaml.safe_dump(raw))
    archive = tmp_path / "all-sections.aqbundle"
    preview = export_bundle(str(archive), str(source), str(data))
    assert set(raw) - {"database", "project_roots", "discord"} <= set(preview["config"])
    target = tmp_path / "local.yaml"
    target.write_text(yaml.safe_dump({
        "messaging_platform": "none",
        "database": {"url": "postgresql+asyncpg://u:p@localhost/aq"},
        "sessions": {"tmux_socket": "/tmp/destination-socket"},
        "reports": {"morning": {"destination": "discord:222222222222222222"}},
        "inbox": {"client_secret": "destination-secret", "oauth_token_path": "/tmp/local-token"},
        "llm": {"api_key": "destination-key"},
        "dashboard_server": {"public_url": "https://destination.local"},
    }))
    before = target.read_text()
    dry = await import_bundle(str(archive), config_path=str(target), data_dir=str(tmp_path / "new"),
                              db=FakeProfileDB(), conflict="replace", dry_run=True)
    assert not dry.get("error"), dry
    assert target.read_text() == before
    result = await import_bundle(str(archive), config_path=str(target), data_dir=str(tmp_path / "new"),
                                 db=FakeProfileDB(), conflict="replace")
    assert result["applied"], result
    local = yaml.safe_load(target.read_text())
    assert local["sessions"] == {"tmux_socket": "/tmp/destination-socket", "lease_ttl_seconds": 777}
    assert local["reports"]["morning"]["destination"] == "discord:222222222222222222"
    assert local["inbox"]["client_secret"] == "destination-secret"
    assert local["inbox"]["oauth_token_path"] == "/tmp/local-token"
    assert local["llm"] == {"api_key": "destination-key", "provider": "openai", "max_tokens": 8000}
    assert local["dashboard_server"] == {"public_url": "https://destination.local", "port": 9002}


async def test_missing_inheritance_dependency_is_rejected_before_writes(tmp_path):
    source, data = _write_source(tmp_path)
    put_text(data, "agent-types/reviewer/profile.md", '''---
id: reviewer
name: Reviewer
extends: missing-template
---
''')
    archive = tmp_path / "missing.aqbundle"
    export_bundle(str(archive), str(source), str(data))
    target = tmp_path / "target.yaml"
    target.write_text("messaging_platform: none\ndatabase:\n  url: postgresql+asyncpg://localhost/target\n")
    before = target.read_text()
    with pytest.raises(PortableBundleError, match="inheritance"):
        await import_bundle(str(archive), config_path=str(target), data_dir=str(tmp_path / "new"),
                            db=FakeProfileDB())
    assert target.read_text() == before
    assert not (tmp_path / "new").exists()


def test_setup_rejects_paths_and_unknown_owned_files(tmp_path):
    source, data = _write_source(tmp_path)
    setup = Setup(system_files={"system/playbooks/../../secrets.md": "private"})
    with pytest.raises(PortableBundleError, match="unsafe"):
        export_bundle(str(tmp_path / "unsafe.aqbundle"), str(source), str(data), setup=setup)
    with pytest.raises(ValueError, match="max_active"):
        PoolSettings(min_active=3, max_active=2)


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
        "version": 1,
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


def test_numeric_knobs_whose_names_contain_token_are_not_treated_as_secrets(tmp_path):
    """``metrics.token_window_seconds`` is a window, not a credential.

    The secret-key heuristic is a substring match, so before this exemption
    any config carrying a ``metrics:`` or ``surface:`` section — which the
    shipped default tuning writes — could not be exported at all.
    """
    config, data_dir = _write_source(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["metrics"] = {"enabled": True, "token_window_seconds": 300.0}
    raw["surface"] = {"context_cost_ceiling_tokens": 8000}
    config.write_text(yaml.safe_dump(raw))

    preview = bundle_preview(str(config), str(data_dir))
    assert preview["config"]["metrics"]["token_window_seconds"] == 300.0
    assert preview["config"]["surface"]["context_cost_ceiling_tokens"] == 8000


def test_a_string_valued_secret_like_key_is_still_refused(tmp_path):
    """The exemption is for scalars only; a credential is a string."""
    config, data_dir = _write_source(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["metrics"] = {"auth_token": "sk-still-a-credential"}
    config.write_text(yaml.safe_dump(raw))

    with pytest.raises(PortableBundleError, match="secret-like key"):
        bundle_preview(str(config), str(data_dir))


def test_integration_travels_as_merge_policy_only(tmp_path):
    """Merge policy is reviewed tuning; the GitHub app is one installation's."""
    config, data_dir = _write_source(tmp_path)
    raw = yaml.safe_load(config.read_text())
    raw["integration"] = {
        "default_mode": "pull_request",
        "merge_ci_policy": "warn",
        "merge_required_checks": ["Tests (default)"],
        "merge_require_up_to_date": True,
        "owner_recovery_sweep": True,
        "github_app": {"app_id": 12345, "installation_id": 999},
        "scratch_probe": {"repository": "someone/private-scratch"},
    }
    config.write_text(yaml.safe_dump(raw))

    preview = bundle_preview(str(config), str(data_dir))
    assert preview["config"]["integration"] == {
        "default_mode": "pull_request",
        "merge_ci_policy": "warn",
        "merge_required_checks": ["Tests (default)"],
        "merge_require_up_to_date": True,
        "owner_recovery_sweep": True,
    }
    # Dropped, and *reported* — an operator can see what did not travel.
    assert "integration.github_app" in preview["excluded_sections"]
    assert "integration.scratch_probe" in preview["excluded_sections"]


def test_import_refuses_a_bundle_carrying_a_non_portable_integration_key(tmp_path):
    """A hand-built bundle cannot smuggle an installation's app id in."""
    bundle = tmp_path / "handmade.aqbundle"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(
            MANIFEST_NAME,
            json.dumps(
                {
                    "format": BUNDLE_FORMAT,
                    "version": 1,
                    "config": CONFIG_NAME,
                    "profiles": [],
                }
            ),
        )
        archive.writestr(
            CONFIG_NAME,
            yaml.safe_dump({"integration": {"default_mode": "direct", "github_app": {"app_id": 7}}}),
        )

    with pytest.raises(PortableBundleError, match="non-portable config sections"):
        inspect_bundle(str(bundle))


@pytest.mark.asyncio
async def test_importing_integration_merges_rather_than_dropping_the_local_github_app(tmp_path):
    """A bundle carries four keys; replacing the section would delete the rest."""
    config, data_dir = _write_source(tmp_path)
    raw = yaml.safe_load(config.read_text())
    # Not "off": ruamel writes that bare and PyYAML reads it back as False.
    raw["integration"] = {"default_mode": "direct", "merge_ci_policy": "required"}
    config.write_text(yaml.safe_dump(raw))
    bundle = tmp_path / "tuning.aqbundle"
    export_bundle(str(bundle), str(config), str(data_dir))

    target = tmp_path / "target.yaml"
    target.write_text(
        yaml.safe_dump(
            {
                "database": {"url": "postgresql+asyncpg://u:p@localhost/aq"},
                "messaging_platform": "none",
                "integration": {
                    "default_mode": "pull_request",
                    "github_app": {
                        "client_id": "Iv1.abc123def456ghi7",
                        "app_id": 4242,
                        "installation_id": 7,
                        "private_key_path": "/run/secrets/aq-github-app.pem",
                    },
                },
            }
        )
    )
    result = await import_bundle(
        str(bundle),
        config_path=str(target),
        data_dir=str(tmp_path / "target-data"),
        db=FakeProfileDB(),
        conflict="replace",
    )
    assert result.get("error") is None, result
    written = yaml.safe_load(target.read_text())["integration"]
    assert written["default_mode"] == "direct"  # the bundle's policy applied
    assert written["merge_ci_policy"] == "required"
    assert written["github_app"]["app_id"] == 4242  # the local app survived
    assert written["github_app"]["private_key_path"] == "/run/secrets/aq-github-app.pem"
