"""Tests for vault directory structure initialization (vault spec §2)
and vault migrations (vault spec §6, Phase 1).
"""

from __future__ import annotations

import time

from src.vault import (
    ORCHESTRATOR_PROFILE,
    PLAYBOOK_TEMPLATE,
    PROFILE_TEMPLATE,
    _STARTER_KNOWLEDGE,
    copy_project_memory_to_vault,
    copy_starter_knowledge,
    ensure_default_playbooks,
    ensure_default_templates,
    ensure_orchestrator_profile,
    ensure_vault_layout,
    ensure_vault_profile_dirs,
    ensure_vault_project_dirs,
    migrate_notes_to_vault,
    migrate_obsidian_config,
    migrate_rule_files,
)


def test_ensure_vault_layout_creates_static_dirs(tmp_path):
    """Static vault directories are created under data_dir."""
    ensure_vault_layout(str(tmp_path))

    expected = [
        "vault/.obsidian",
        "vault/system/playbooks",
        "vault/system/memory",
        "vault/orchestrator/playbooks",
        "vault/orchestrator/memory",
        "vault/agent-types",
        "vault/projects",
        "vault/templates",
    ]
    for subdir in expected:
        assert (tmp_path / subdir).is_dir(), f"Missing directory: {subdir}"


def test_ensure_vault_layout_idempotent(tmp_path):
    """Calling ensure_vault_layout twice does not raise."""
    ensure_vault_layout(str(tmp_path))
    ensure_vault_layout(str(tmp_path))

    assert (tmp_path / "vault" / "system" / "playbooks").is_dir()


def test_ensure_vault_profile_dirs(tmp_path):
    """Per-profile vault directories are created correctly."""
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    base = tmp_path / "vault" / "agent-types" / "coding"
    assert (base / "playbooks").is_dir()
    assert (base / "memory").is_dir()


def test_ensure_vault_profile_dirs_idempotent(tmp_path):
    """Calling ensure_vault_profile_dirs twice does not raise."""
    ensure_vault_profile_dirs(str(tmp_path), "coding")
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    assert (tmp_path / "vault" / "agent-types" / "coding" / "playbooks").is_dir()


def test_ensure_vault_project_dirs(tmp_path):
    """Per-project vault directories are created with full subtree."""
    ensure_vault_project_dirs(str(tmp_path), "mech-fighters")

    base = tmp_path / "vault" / "projects" / "mech-fighters"
    expected_subdirs = [
        "memory/knowledge",
        "memory/insights",
        "playbooks",
        "notes",
        "references",
        "overrides",
    ]
    for subdir in expected_subdirs:
        assert (base / subdir).is_dir(), f"Missing project subdir: {subdir}"


def test_ensure_vault_project_dirs_idempotent(tmp_path):
    """Calling ensure_vault_project_dirs twice does not raise."""
    ensure_vault_project_dirs(str(tmp_path), "my-project")
    ensure_vault_project_dirs(str(tmp_path), "my-project")

    assert (tmp_path / "vault" / "projects" / "my-project" / "playbooks").is_dir()


# ---------------------------------------------------------------------------
# migrate_obsidian_config (vault spec §6, Phase 1)
# ---------------------------------------------------------------------------


def test_migrate_obsidian_config_moves_directory(tmp_path):
    """When source exists and destination does not, .obsidian is moved."""
    source = tmp_path / "memory" / ".obsidian"
    source.mkdir(parents=True)
    # Populate with representative Obsidian config files
    (source / "app.json").write_text('{"theme": "dark"}')
    (source / "workspace.json").write_text('{"active": "notes"}')
    plugins_dir = source / "plugins" / "dataview"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "main.js").write_text("// plugin code")

    result = migrate_obsidian_config(str(tmp_path))

    assert result is True
    dest = tmp_path / "vault" / ".obsidian"
    assert dest.is_dir()
    assert (dest / "app.json").read_text() == '{"theme": "dark"}'
    assert (dest / "workspace.json").read_text() == '{"active": "notes"}'
    assert (dest / "plugins" / "dataview" / "main.js").read_text() == "// plugin code"
    # Source should no longer exist
    assert not source.exists()


def test_migrate_obsidian_config_skips_when_source_missing(tmp_path):
    """When source does not exist, nothing happens."""
    result = migrate_obsidian_config(str(tmp_path))

    assert result is False
    # Destination should not have been created either
    assert not (tmp_path / "vault" / ".obsidian").exists()


def test_migrate_obsidian_config_skips_when_dest_exists(tmp_path):
    """When destination already exists, source is left untouched."""
    source = tmp_path / "memory" / ".obsidian"
    source.mkdir(parents=True)
    (source / "app.json").write_text('{"theme": "dark"}')

    dest = tmp_path / "vault" / ".obsidian"
    dest.mkdir(parents=True)
    (dest / "existing.json").write_text('{"keep": true}')

    result = migrate_obsidian_config(str(tmp_path))

    assert result is False
    # Source still exists (not moved)
    assert source.is_dir()
    assert (source / "app.json").read_text() == '{"theme": "dark"}'
    # Destination retains its original content
    assert (dest / "existing.json").read_text() == '{"keep": true}'


def test_migrate_obsidian_config_idempotent(tmp_path):
    """Calling migrate twice — the second call is a no-op."""
    source = tmp_path / "memory" / ".obsidian"
    source.mkdir(parents=True)
    (source / "app.json").write_text('{"theme": "dark"}')

    assert migrate_obsidian_config(str(tmp_path)) is True
    # Source gone after first call; second call should skip gracefully
    assert migrate_obsidian_config(str(tmp_path)) is False

    dest = tmp_path / "vault" / ".obsidian"
    assert dest.is_dir()
    assert (dest / "app.json").read_text() == '{"theme": "dark"}'


def test_migrate_obsidian_config_creates_vault_parent(tmp_path):
    """Vault parent directory is created if it doesn't exist yet."""
    source = tmp_path / "memory" / ".obsidian"
    source.mkdir(parents=True)
    (source / "themes.json").write_text("{}")

    # No vault/ directory exists yet
    assert not (tmp_path / "vault").exists()

    result = migrate_obsidian_config(str(tmp_path))

    assert result is True
    assert (tmp_path / "vault" / ".obsidian" / "themes.json").read_text() == "{}"


def test_multiple_profiles_and_projects(tmp_path):
    """Multiple profiles and projects can coexist under vault."""
    ensure_vault_layout(str(tmp_path))

    for profile_id in ("coding", "code-review", "qa"):
        ensure_vault_profile_dirs(str(tmp_path), profile_id)

    for project_id in ("project-a", "project-b"):
        ensure_vault_project_dirs(str(tmp_path), project_id)

    # Verify all exist
    agent_types = tmp_path / "vault" / "agent-types"
    assert (agent_types / "coding" / "playbooks").is_dir()
    assert (agent_types / "code-review" / "memory").is_dir()
    assert (agent_types / "qa" / "playbooks").is_dir()

    projects = tmp_path / "vault" / "projects"
    assert (projects / "project-a" / "overrides").is_dir()
    assert (projects / "project-b" / "references").is_dir()


# migrate_notes_to_vault (vault spec §6, Phase 1)
# ---------------------------------------------------------------------------


def test_migrate_notes_moves_files(tmp_path):
    """Markdown files are moved from notes/{id}/ to vault/projects/{id}/notes/."""
    source = tmp_path / "notes" / "my-proj"
    source.mkdir(parents=True)
    (source / "design.md").write_text("# Design Notes")
    (source / "roadmap.md").write_text("# Roadmap")

    # Ensure vault project dirs exist (as orchestrator would do)
    ensure_vault_project_dirs(str(tmp_path), "my-proj")

    result = migrate_notes_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "notes"
    assert (dest / "design.md").read_text() == "# Design Notes"
    assert (dest / "roadmap.md").read_text() == "# Roadmap"
    # Source directory should be cleaned up
    assert not source.exists()


def test_migrate_notes_preserves_subdirectory_structure(tmp_path):
    """Nested subdirectories inside notes are preserved at the destination."""
    source = tmp_path / "notes" / "my-proj"
    sub = source / "category"
    sub.mkdir(parents=True)
    (source / "top.md").write_text("top-level")
    (sub / "nested.md").write_text("nested note")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    result = migrate_notes_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "notes"
    assert (dest / "top.md").read_text() == "top-level"
    assert (dest / "category" / "nested.md").read_text() == "nested note"
    assert not source.exists()


def test_migrate_notes_skips_when_source_missing(tmp_path):
    """When the source notes directory does not exist, nothing happens."""
    result = migrate_notes_to_vault(str(tmp_path), "no-such-project")

    assert result is False
    # Destination should not be created by the migration itself
    assert not (tmp_path / "vault" / "projects" / "no-such-project" / "notes").exists()


def test_migrate_notes_skips_existing_dest_files(tmp_path):
    """Files already at the destination are not overwritten."""
    source = tmp_path / "notes" / "my-proj"
    source.mkdir(parents=True)
    (source / "existing.md").write_text("old version")
    (source / "new-note.md").write_text("brand new")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    dest = tmp_path / "vault" / "projects" / "my-proj" / "notes"
    (dest / "existing.md").write_text("already migrated")

    result = migrate_notes_to_vault(str(tmp_path), "my-proj")

    assert result is True  # new-note.md was moved
    # The pre-existing file at dest should NOT be overwritten
    assert (dest / "existing.md").read_text() == "already migrated"
    assert (dest / "new-note.md").read_text() == "brand new"


def test_migrate_notes_idempotent(tmp_path):
    """Calling migrate twice — the second call is a no-op."""
    source = tmp_path / "notes" / "my-proj"
    source.mkdir(parents=True)
    (source / "note.md").write_text("content")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")

    assert migrate_notes_to_vault(str(tmp_path), "my-proj") is True
    # Source is gone — second call should skip
    assert migrate_notes_to_vault(str(tmp_path), "my-proj") is False

    dest = tmp_path / "vault" / "projects" / "my-proj" / "notes"
    assert (dest / "note.md").read_text() == "content"


def test_migrate_notes_creates_dest_dir(tmp_path):
    """Destination vault directories are created if they don't exist yet."""
    source = tmp_path / "notes" / "my-proj"
    source.mkdir(parents=True)
    (source / "note.md").write_text("content")

    # Do NOT call ensure_vault_project_dirs — migration should handle it
    assert not (tmp_path / "vault").exists()

    result = migrate_notes_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "notes"
    assert (dest / "note.md").read_text() == "content"


def test_migrate_notes_moves_non_markdown_files(tmp_path):
    """All files are moved, not just .md files."""
    source = tmp_path / "notes" / "my-proj"
    source.mkdir(parents=True)
    (source / "note.md").write_text("markdown")
    (source / "data.json").write_text('{"key": "value"}')

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    result = migrate_notes_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "notes"
    assert (dest / "note.md").read_text() == "markdown"
    assert (dest / "data.json").read_text() == '{"key": "value"}'


# ---------------------------------------------------------------------------
# copy_project_memory_to_vault (vault spec §6, Phase 1)
# ---------------------------------------------------------------------------


def test_copy_memory_copies_profile_and_factsheet(tmp_path):
    """profile.md and factsheet.md are copied to vault memory directory."""
    source = tmp_path / "memory" / "my-proj"
    source.mkdir(parents=True)
    (source / "profile.md").write_text("# Project Profile")
    (source / "factsheet.md").write_text("# Factsheet")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory"
    assert (dest / "profile.md").read_text() == "# Project Profile"
    assert (dest / "factsheet.md").read_text() == "# Factsheet"
    # Source files should still exist (copy, not move)
    assert (source / "profile.md").read_text() == "# Project Profile"
    assert (source / "factsheet.md").read_text() == "# Factsheet"


def test_copy_memory_copies_knowledge_dir(tmp_path):
    """knowledge/ directory contents are recursively copied to vault."""
    source = tmp_path / "memory" / "my-proj"
    knowledge = source / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "architecture.md").write_text("# Architecture")
    (knowledge / "decisions.md").write_text("# Decisions")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory" / "knowledge"
    assert (dest / "architecture.md").read_text() == "# Architecture"
    assert (dest / "decisions.md").read_text() == "# Decisions"
    # Source files should still exist
    assert (knowledge / "architecture.md").exists()
    assert (knowledge / "decisions.md").exists()


def test_copy_memory_copies_knowledge_subdirs(tmp_path):
    """Nested subdirectories inside knowledge/ are preserved."""
    source = tmp_path / "memory" / "my-proj"
    sub = source / "knowledge" / "deep" / "nested"
    sub.mkdir(parents=True)
    (sub / "topic.md").write_text("deep topic")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory" / "knowledge"
    assert (dest / "deep" / "nested" / "topic.md").read_text() == "deep topic"


def test_copy_memory_skips_when_source_missing(tmp_path):
    """When memory/{project_id}/ does not exist, nothing happens."""
    result = copy_project_memory_to_vault(str(tmp_path), "nonexistent")

    assert result is False


def test_copy_memory_skips_up_to_date_files(tmp_path):
    """Files already at the destination with same or newer mtime are skipped."""
    source = tmp_path / "memory" / "my-proj"
    source.mkdir(parents=True)
    (source / "profile.md").write_text("old version")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory"
    (dest / "profile.md").write_text("already copied version")

    # Make dest file newer than source
    import os

    src_file = str(source / "profile.md")
    dst_file = str(dest / "profile.md")
    now = time.time()
    os.utime(src_file, (now - 10, now - 10))
    os.utime(dst_file, (now, now))

    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is False
    # Destination should retain its own content
    assert (dest / "profile.md").read_text() == "already copied version"


def test_copy_memory_updates_when_source_newer(tmp_path):
    """When source is newer than destination, the destination is updated."""
    source = tmp_path / "memory" / "my-proj"
    source.mkdir(parents=True)

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory"

    # Write destination first with older timestamp
    (dest / "profile.md").write_text("stale version")
    import os

    now = time.time()
    os.utime(str(dest / "profile.md"), (now - 10, now - 10))

    # Write source with newer timestamp
    (source / "profile.md").write_text("updated version")
    os.utime(str(source / "profile.md"), (now, now))

    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is True
    assert (dest / "profile.md").read_text() == "updated version"


def test_copy_memory_idempotent(tmp_path):
    """Calling copy twice — second call is a no-op when files haven't changed."""
    source = tmp_path / "memory" / "my-proj"
    source.mkdir(parents=True)
    (source / "profile.md").write_text("# Profile")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")

    assert copy_project_memory_to_vault(str(tmp_path), "my-proj") is True
    # Second call: dest now has same mtime (via copy2), so should be skipped
    assert copy_project_memory_to_vault(str(tmp_path), "my-proj") is False


def test_copy_memory_excludes_tasks_and_rules(tmp_path):
    """tasks/ and rules/ directories are NOT copied to the vault."""
    source = tmp_path / "memory" / "my-proj"
    (source / "tasks").mkdir(parents=True)
    (source / "rules").mkdir(parents=True)
    (source / "tasks" / "task-001.md").write_text("task record")
    (source / "rules" / "rule-001.yaml").write_text("rule content")
    (source / "profile.md").write_text("# Profile")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory"
    assert (dest / "profile.md").read_text() == "# Profile"
    # tasks/ and rules/ should NOT exist in vault memory
    assert not (dest / "tasks").exists()
    assert not (dest / "rules").exists()


def test_copy_memory_handles_partial_files(tmp_path):
    """Only existing files are copied — missing profile.md or factsheet.md is fine."""
    source = tmp_path / "memory" / "my-proj"
    source.mkdir(parents=True)
    # Only factsheet, no profile.md
    (source / "factsheet.md").write_text("# Facts")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory"
    assert (dest / "factsheet.md").read_text() == "# Facts"
    assert not (dest / "profile.md").exists()


def test_copy_memory_creates_dest_dir(tmp_path):
    """Destination vault directories are created if they don't exist yet."""
    source = tmp_path / "memory" / "my-proj"
    source.mkdir(parents=True)
    (source / "profile.md").write_text("# Profile")

    # Do NOT call ensure_vault_project_dirs — copy should handle it
    assert not (tmp_path / "vault").exists()

    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory"
    assert (dest / "profile.md").read_text() == "# Profile"


def test_copy_memory_all_files_together(tmp_path):
    """Full scenario: profile.md + factsheet.md + knowledge/ all copied."""
    source = tmp_path / "memory" / "my-proj"
    knowledge = source / "knowledge"
    knowledge.mkdir(parents=True)
    (source / "profile.md").write_text("# Profile")
    (source / "factsheet.md").write_text("# Factsheet")
    (knowledge / "arch.md").write_text("# Architecture")
    (knowledge / "conventions.md").write_text("# Conventions")

    ensure_vault_project_dirs(str(tmp_path), "my-proj")
    result = copy_project_memory_to_vault(str(tmp_path), "my-proj")

    assert result is True
    dest = tmp_path / "vault" / "projects" / "my-proj" / "memory"
    assert (dest / "profile.md").read_text() == "# Profile"
    assert (dest / "factsheet.md").read_text() == "# Factsheet"
    assert (dest / "knowledge" / "arch.md").read_text() == "# Architecture"
    assert (dest / "knowledge" / "conventions.md").read_text() == "# Conventions"
    # Source files still exist
    assert (source / "profile.md").exists()
    assert (source / "factsheet.md").exists()
    assert (knowledge / "arch.md").exists()


# ---------------------------------------------------------------------------
# migrate_rule_files (vault spec §6, Phase 1)
# ---------------------------------------------------------------------------


def _create_rule_file(path, rule_id="rule-test", rule_type="active"):
    """Helper: write a minimal rule markdown file with frontmatter."""
    content = (
        f"---\nid: {rule_id}\ntype: {rule_type}\nproject_id: null\n"
        f"hooks: []\ncreated: '2026-01-01T00:00:00+00:00'\n"
        f"updated: '2026-01-01T00:00:00+00:00'\n---\n\n"
        f"# {rule_id.replace('rule-', '').replace('-', ' ').title()}\n\n"
        f"## Intent\nTest rule.\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return content


def test_migrate_rule_files_moves_global_rules(tmp_path):
    """Global rules are moved from memory/global/rules/ to vault/system/playbooks/."""
    # Set up source
    src = tmp_path / "memory" / "global" / "rules" / "rule-test.md"
    content = _create_rule_file(src)

    # Create vault destination dir
    ensure_vault_layout(str(tmp_path))

    result = migrate_rule_files(str(tmp_path))

    assert result["moved"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == 0

    # File should be at new location
    dest = tmp_path / "vault" / "system" / "playbooks" / "rule-test.md"
    assert dest.is_file()
    assert dest.read_text() == content

    # Source should be gone
    assert not src.exists()


def test_migrate_rule_files_moves_project_rules(tmp_path):
    """Project rules are moved from memory/{pid}/rules/ to vault/projects/{pid}/playbooks/."""
    src = tmp_path / "memory" / "my-project" / "rules" / "rule-check.md"
    content = _create_rule_file(src, rule_id="rule-check")

    ensure_vault_layout(str(tmp_path))
    ensure_vault_project_dirs(str(tmp_path), "my-project")

    result = migrate_rule_files(str(tmp_path))

    assert result["moved"] == 1

    dest = tmp_path / "vault" / "projects" / "my-project" / "playbooks" / "rule-check.md"
    assert dest.is_file()
    assert dest.read_text() == content
    assert not src.exists()


def test_migrate_rule_files_skips_existing_destination(tmp_path):
    """Files already at destination are skipped (idempotent)."""
    src = tmp_path / "memory" / "global" / "rules" / "rule-dup.md"
    _create_rule_file(src, rule_id="rule-dup")

    # Pre-create the destination file with different content
    dest = tmp_path / "vault" / "system" / "playbooks" / "rule-dup.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("# Already here\n")

    result = migrate_rule_files(str(tmp_path))

    assert result["moved"] == 0
    assert result["skipped"] == 1

    # Destination retains original content
    assert dest.read_text() == "# Already here\n"
    # Source is still present (not removed)
    assert src.exists()


def test_migrate_rule_files_idempotent(tmp_path):
    """Running migration twice: first moves, second is a no-op."""
    src = tmp_path / "memory" / "global" / "rules" / "rule-idem.md"
    _create_rule_file(src, rule_id="rule-idem")
    ensure_vault_layout(str(tmp_path))

    result1 = migrate_rule_files(str(tmp_path))
    assert result1["moved"] == 1

    # Source gone after first run; second run should skip gracefully
    result2 = migrate_rule_files(str(tmp_path))
    assert result2["moved"] == 0
    assert result2["skipped"] == 0  # no source files left

    # File still at destination
    assert (tmp_path / "vault" / "system" / "playbooks" / "rule-idem.md").is_file()


def test_migrate_rule_files_multiple_scopes(tmp_path):
    """Rules from multiple scopes (global + projects) are all migrated."""
    # Global rules
    _create_rule_file(
        tmp_path / "memory" / "global" / "rules" / "rule-a.md",
        rule_id="rule-a",
    )
    _create_rule_file(
        tmp_path / "memory" / "global" / "rules" / "rule-b.md",
        rule_id="rule-b",
    )
    # Project rules
    _create_rule_file(
        tmp_path / "memory" / "proj-1" / "rules" / "rule-c.md",
        rule_id="rule-c",
    )
    _create_rule_file(
        tmp_path / "memory" / "proj-2" / "rules" / "rule-d.md",
        rule_id="rule-d",
    )

    ensure_vault_layout(str(tmp_path))
    ensure_vault_project_dirs(str(tmp_path), "proj-1")
    ensure_vault_project_dirs(str(tmp_path), "proj-2")

    result = migrate_rule_files(str(tmp_path))

    assert result["moved"] == 4
    assert result["errors"] == 0
    assert len(result["details"]) == 4

    # Verify all destinations
    assert (tmp_path / "vault" / "system" / "playbooks" / "rule-a.md").is_file()
    assert (tmp_path / "vault" / "system" / "playbooks" / "rule-b.md").is_file()
    assert (tmp_path / "vault" / "projects" / "proj-1" / "playbooks" / "rule-c.md").is_file()
    assert (tmp_path / "vault" / "projects" / "proj-2" / "playbooks" / "rule-d.md").is_file()


def test_migrate_rule_files_no_memory_dir(tmp_path):
    """When memory directory doesn't exist, returns empty stats."""
    result = migrate_rule_files(str(tmp_path))

    assert result["moved"] == 0
    assert result["skipped"] == 0
    assert result["errors"] == 0


def test_migrate_rule_files_ignores_non_md_files(tmp_path):
    """Non-.md files in rules/ directories are not migrated."""
    rules_dir = tmp_path / "memory" / "global" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "rule-good.md").write_text("# Good rule\n")
    (rules_dir / "notes.txt").write_text("not a rule")
    (rules_dir / ".hidden").write_text("hidden file")

    ensure_vault_layout(str(tmp_path))
    result = migrate_rule_files(str(tmp_path))

    assert result["moved"] == 1
    # Non-md files still in source
    assert (rules_dir / "notes.txt").exists()
    assert (rules_dir / ".hidden").exists()


def test_migrate_rule_files_creates_dest_dirs(tmp_path):
    """Destination directories are created if they don't exist yet."""
    src = tmp_path / "memory" / "new-project" / "rules" / "rule-x.md"
    _create_rule_file(src, rule_id="rule-x")

    # Don't call ensure_vault_layout — migration should create dest dirs
    result = migrate_rule_files(str(tmp_path))

    assert result["moved"] == 1
    dest = tmp_path / "vault" / "projects" / "new-project" / "playbooks" / "rule-x.md"
    assert dest.is_file()


def test_migrate_rule_files_preserves_content(tmp_path):
    """Migrated files preserve exact content including frontmatter."""
    src = tmp_path / "memory" / "global" / "rules" / "rule-preserve.md"
    content = (
        "---\nid: rule-preserve\ntype: active\nproject_id: null\n"
        "hooks:\n- hook-abc123\n- hook-def456\n"
        "created: '2026-03-15T12:00:00+00:00'\n"
        "updated: '2026-04-01T08:30:00+00:00'\n---\n\n"
        "# Preserve Test\n\n## Intent\nPreserve all content exactly.\n\n"
        "## Trigger\nEvery 2 hours.\n\n## Logic\n1. Do the thing.\n2. Report.\n"
    )
    src.parent.mkdir(parents=True)
    src.write_text(content)

    ensure_vault_layout(str(tmp_path))
    migrate_rule_files(str(tmp_path))

    dest = tmp_path / "vault" / "system" / "playbooks" / "rule-preserve.md"
    assert dest.read_text() == content


def test_migrate_rule_files_logs_details(tmp_path):
    """Each move/skip is recorded in the details list."""
    _create_rule_file(
        tmp_path / "memory" / "global" / "rules" / "rule-moved.md",
        rule_id="rule-moved",
    )
    dest = tmp_path / "vault" / "system" / "playbooks" / "rule-skipped.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("# existing\n")
    _create_rule_file(
        tmp_path / "memory" / "global" / "rules" / "rule-skipped.md",
        rule_id="rule-skipped",
    )

    result = migrate_rule_files(str(tmp_path))

    assert result["moved"] == 1
    assert result["skipped"] == 1
    # Details should contain entries for both files
    details_text = "\n".join(result["details"])
    assert "MOVE" in details_text
    assert "SKIP" in details_text


# ---------------------------------------------------------------------------
# Default template tests (roadmap §4.2.2)
# ---------------------------------------------------------------------------


def test_ensure_default_templates_creates_all_files(tmp_path):
    """ensure_default_templates creates profile, playbook, and starter knowledge files."""
    result = ensure_default_templates(str(tmp_path))

    # Profile and playbook templates
    assert (tmp_path / "vault" / "templates" / "profile-template.md").is_file()
    assert (tmp_path / "vault" / "templates" / "playbook-template.md").is_file()

    # Starter knowledge packs
    for agent_type, files in _STARTER_KNOWLEDGE.items():
        for filename in files:
            path = tmp_path / "vault" / "templates" / "knowledge" / agent_type / filename
            assert path.is_file(), f"Missing starter knowledge: {path}"

    # All files should be in the created list
    assert "profile-template.md" in result["created"]
    assert "playbook-template.md" in result["created"]
    assert len(result["skipped"]) == 0


def test_ensure_default_templates_idempotent(tmp_path):
    """Calling ensure_default_templates twice does not overwrite existing files."""
    ensure_default_templates(str(tmp_path))

    # Modify a template to verify it's not overwritten
    profile_path = tmp_path / "vault" / "templates" / "profile-template.md"
    custom_content = "# My Custom Profile Template\n"
    profile_path.write_text(custom_content)

    result = ensure_default_templates(str(tmp_path))

    # The customized file should be preserved
    assert profile_path.read_text() == custom_content
    assert "profile-template.md" in result["skipped"]


def test_ensure_default_templates_partial_existing(tmp_path):
    """Only missing templates are created; existing ones are skipped."""
    templates_dir = tmp_path / "vault" / "templates"
    templates_dir.mkdir(parents=True)

    # Pre-create just the profile template
    profile_path = templates_dir / "profile-template.md"
    profile_path.write_text("# existing\n")

    result = ensure_default_templates(str(tmp_path))

    # Profile should be skipped, playbook should be created
    assert "profile-template.md" in result["skipped"]
    assert "playbook-template.md" in result["created"]
    assert profile_path.read_text() == "# existing\n"
    assert (templates_dir / "playbook-template.md").is_file()


def test_ensure_vault_layout_creates_templates(tmp_path):
    """ensure_vault_layout creates default templates as part of the layout."""
    ensure_vault_layout(str(tmp_path))

    assert (tmp_path / "vault" / "templates" / "profile-template.md").is_file()
    assert (tmp_path / "vault" / "templates" / "playbook-template.md").is_file()
    # Starter knowledge packs are also created
    assert (tmp_path / "vault" / "templates" / "knowledge" / "coding").is_dir()
    assert (tmp_path / "vault" / "templates" / "knowledge" / "code-review").is_dir()
    assert (tmp_path / "vault" / "templates" / "knowledge" / "qa").is_dir()


def test_ensure_vault_layout_preserves_custom_templates(tmp_path):
    """ensure_vault_layout does not overwrite user-customised templates."""
    # First call creates defaults
    ensure_vault_layout(str(tmp_path))

    # User customises a template
    profile_path = tmp_path / "vault" / "templates" / "profile-template.md"
    custom = "# User's custom template\n"
    profile_path.write_text(custom)

    # Second call should not overwrite
    ensure_vault_layout(str(tmp_path))
    assert profile_path.read_text() == custom


def test_profile_template_content_is_valid():
    """The default profile template parses without errors."""
    from src.profile_parser import parse_profile

    result = parse_profile(PROFILE_TEMPLATE)
    assert result.is_valid, f"Profile template has parse errors: {result.errors}"
    assert result.frontmatter.id == "my-agent"
    assert result.frontmatter.name == "My Agent"
    assert "profile" in result.frontmatter.tags
    assert "agent-type" in result.frontmatter.tags


def test_profile_template_has_all_sections():
    """The default profile template includes all documented sections."""
    from src.profile_parser import parse_profile

    result = parse_profile(PROFILE_TEMPLATE)
    assert result.is_valid

    # Structured sections
    assert "config" in result.sections
    assert "tools" in result.sections
    assert "mcp servers" in result.sections
    assert "install" in result.sections

    # Prompt sections
    assert "role" in result.sections
    assert "rules" in result.sections
    assert "reflection" in result.sections


def test_playbook_template_has_frontmatter():
    """The default playbook template has valid YAML frontmatter with id and triggers."""
    import yaml

    # Extract frontmatter
    lines = PLAYBOOK_TEMPLATE.strip().splitlines()
    assert lines[0] == "---"
    end_idx = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
    fm_text = "\n".join(lines[1:end_idx])
    fm = yaml.safe_load(fm_text)

    assert fm["id"] == "my-playbook"
    assert isinstance(fm["triggers"], list)
    assert len(fm["triggers"]) > 0
    assert "scope" in fm


def test_starter_knowledge_files_have_frontmatter(tmp_path):
    """All starter knowledge files have valid YAML frontmatter with tags."""
    import yaml

    ensure_default_templates(str(tmp_path))

    for agent_type, files in _STARTER_KNOWLEDGE.items():
        for filename in files:
            path = tmp_path / "vault" / "templates" / "knowledge" / agent_type / filename
            content = path.read_text()

            # Must start with YAML frontmatter
            assert content.startswith("---"), f"{agent_type}/{filename} missing frontmatter"
            # Extract and parse frontmatter
            lines = content.strip().splitlines()
            end_idx = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
            fm_text = "\n".join(lines[1:end_idx])
            fm = yaml.safe_load(fm_text)
            assert "tags" in fm, f"{agent_type}/{filename} missing tags"
            assert "starter" in fm["tags"], f"{agent_type}/{filename} missing 'starter' tag"


def test_starter_knowledge_covers_expected_types():
    """Starter knowledge packs exist for coding, code-review, and qa agent types."""
    assert "coding" in _STARTER_KNOWLEDGE
    assert "code-review" in _STARTER_KNOWLEDGE
    assert "qa" in _STARTER_KNOWLEDGE

    # Coding has two files per profiles spec §4
    assert "common-pitfalls.md" in _STARTER_KNOWLEDGE["coding"]
    assert "git-conventions.md" in _STARTER_KNOWLEDGE["coding"]

    # Code-review has review checklist
    assert "review-checklist.md" in _STARTER_KNOWLEDGE["code-review"]

    # QA has testing patterns and process
    assert "testing-patterns.md" in _STARTER_KNOWLEDGE["qa"]
    assert "qa-process.md" in _STARTER_KNOWLEDGE["qa"]


# ---------------------------------------------------------------------------
# Default playbook tests (playbooks spec §12)
# ---------------------------------------------------------------------------


def test_ensure_default_playbooks_creates_task_outcome(tmp_path):
    """ensure_default_playbooks installs task-outcome.md to vault/system/playbooks/."""
    result = ensure_default_playbooks(str(tmp_path))

    playbook_path = tmp_path / "vault" / "system" / "playbooks" / "task-outcome.md"
    assert playbook_path.is_file()
    assert "task-outcome.md" in result["created"]
    assert len(result["skipped"]) == 0


def test_ensure_default_playbooks_idempotent(tmp_path):
    """Calling ensure_default_playbooks twice does not overwrite existing files."""
    ensure_default_playbooks(str(tmp_path))

    playbook_path = tmp_path / "vault" / "system" / "playbooks" / "task-outcome.md"
    custom_content = "---\nid: task-outcome\ntriggers:\n  - task.completed\nscope: system\n---\n"
    playbook_path.write_text(custom_content)

    result = ensure_default_playbooks(str(tmp_path))

    assert playbook_path.read_text() == custom_content
    assert "task-outcome.md" in result["skipped"]


def test_ensure_default_playbooks_partial_existing(tmp_path):
    """Only missing playbooks are installed; existing ones are skipped."""
    playbooks_dir = tmp_path / "vault" / "system" / "playbooks"
    playbooks_dir.mkdir(parents=True)

    # Pre-create the task-outcome playbook
    existing = playbooks_dir / "task-outcome.md"
    existing.write_text("# customised\n")

    result = ensure_default_playbooks(str(tmp_path))

    assert "task-outcome.md" in result["skipped"]
    assert existing.read_text() == "# customised\n"
    # Other playbooks should still be created since they didn't exist
    assert "codebase-inspector.md" in result["created"]
    assert "dependency-audit.md" in result["created"]


def test_task_outcome_playbook_has_valid_frontmatter():
    """The bundled task-outcome.md has valid YAML frontmatter with required fields."""
    import os

    import yaml

    playbook_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "src",
        "prompts",
        "default_playbooks",
        "task-outcome.md",
    )
    content = open(playbook_path).read()

    # Must start with YAML frontmatter
    assert content.startswith("---")
    lines = content.strip().splitlines()
    end_idx = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
    fm_text = "\n".join(lines[1:end_idx])
    fm = yaml.safe_load(fm_text)

    assert fm["id"] == "task-outcome"
    assert isinstance(fm["triggers"], list)
    assert "task.completed" in fm["triggers"]
    assert "task.failed" in fm["triggers"]
    assert fm["scope"] == "system"


def test_task_outcome_playbook_consolidates_three_concerns():
    """The task-outcome playbook body addresses reflection, spec-drift, and error-recovery."""
    import os

    playbook_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "src",
        "prompts",
        "default_playbooks",
        "task-outcome.md",
    )
    content = open(playbook_path).read()

    # Extract body (after frontmatter)
    parts = content.split("---", 2)
    body = parts[2].strip()

    # Reflection concerns: acceptance criteria review
    assert "acceptance criteria" in body.lower()

    # Spec-drift concerns: spec divergence detection
    assert "spec" in body.lower()

    # Error-recovery concerns: transient errors, retry, fix tasks
    assert "retry" in body.lower()
    assert "transient" in body.lower() or "rate limit" in body.lower()
    assert "fix task" in body.lower() or "create" in body.lower()


def test_ensure_default_playbooks_creates_codebase_inspector(tmp_path):
    """ensure_default_playbooks installs codebase-inspector.md to vault/system/playbooks/."""
    result = ensure_default_playbooks(str(tmp_path))

    playbook_path = tmp_path / "vault" / "system" / "playbooks" / "codebase-inspector.md"
    assert playbook_path.is_file()
    assert "codebase-inspector.md" in result["created"]


def test_codebase_inspector_playbook_has_valid_frontmatter():
    """The bundled codebase-inspector.md has valid YAML frontmatter with required fields."""
    import os

    import yaml

    playbook_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "src",
        "prompts",
        "default_playbooks",
        "codebase-inspector.md",
    )
    content = open(playbook_path).read()

    # Must start with YAML frontmatter
    assert content.startswith("---")
    lines = content.strip().splitlines()
    end_idx = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
    fm_text = "\n".join(lines[1:end_idx])
    fm = yaml.safe_load(fm_text)

    assert fm["id"] == "codebase-inspector"
    assert isinstance(fm["triggers"], list)
    assert "timer.4h" in fm["triggers"]
    assert fm["scope"] == "system"


def test_codebase_inspector_playbook_covers_key_concerns():
    """The codebase-inspector playbook body covers weighted selection and dedup."""
    import os

    playbook_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "src",
        "prompts",
        "default_playbooks",
        "codebase-inspector.md",
    )
    content = open(playbook_path).read()

    # Extract body (after frontmatter)
    parts = content.split("---", 2)
    body = parts[2].strip().lower()

    # Weighted selection categories from spec
    assert "source" in body
    assert "spec" in body
    assert "test" in body
    assert "config" in body
    assert "recent" in body

    # Quality concerns
    assert "quality" in body
    assert "security" in body
    assert "documentation" in body or "doc" in body

    # Inspection history dedup
    assert "inspection history" in body or "re-inspect" in body

    # Actionable findings only
    assert "actionable" in body

    # Consolidation with health check
    assert "health check" in body or "consolidate" in body
    assert "duplicate" in body


def test_ensure_default_playbooks_idempotent_codebase_inspector(tmp_path):
    """Calling ensure_default_playbooks twice does not overwrite codebase-inspector.md."""
    ensure_default_playbooks(str(tmp_path))

    playbook_path = tmp_path / "vault" / "system" / "playbooks" / "codebase-inspector.md"
    custom_content = "---\nid: codebase-inspector\ntriggers:\n  - timer.4h\nscope: system\n---\n"
    playbook_path.write_text(custom_content)

    result = ensure_default_playbooks(str(tmp_path))

    assert playbook_path.read_text() == custom_content
    assert "codebase-inspector.md" in result["skipped"]


def test_ensure_default_playbooks_creates_dependency_audit(tmp_path):
    """ensure_default_playbooks installs dependency-audit.md to vault/system/playbooks/."""
    result = ensure_default_playbooks(str(tmp_path))

    playbook_path = tmp_path / "vault" / "system" / "playbooks" / "dependency-audit.md"
    assert playbook_path.is_file()
    assert "dependency-audit.md" in result["created"]


def test_dependency_audit_playbook_has_valid_frontmatter():
    """The bundled dependency-audit.md has valid YAML frontmatter with required fields."""
    import os

    import yaml

    playbook_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "src",
        "prompts",
        "default_playbooks",
        "dependency-audit.md",
    )
    content = open(playbook_path).read()

    # Must start with YAML frontmatter
    assert content.startswith("---")
    lines = content.strip().splitlines()
    end_idx = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
    fm_text = "\n".join(lines[1:end_idx])
    fm = yaml.safe_load(fm_text)

    assert fm["id"] == "dependency-audit"
    assert isinstance(fm["triggers"], list)
    assert "timer.24h" in fm["triggers"]
    assert fm["scope"] == "system"


def test_dependency_audit_playbook_covers_key_concerns():
    """The dependency-audit playbook body covers pip-audit, check-outdated-deps, and triage."""
    import os

    playbook_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "src",
        "prompts",
        "default_playbooks",
        "dependency-audit.md",
    )
    content = open(playbook_path).read()

    # Extract body (after frontmatter)
    parts = content.split("---", 2)
    body = parts[2].strip().lower()

    # Audit tooling
    assert "pip-audit" in body
    assert "check-outdated-deps" in body

    # Triage: critical vs non-critical
    assert "critical" in body
    assert "high-priority" in body or "high priority" in body
    assert "task" in body  # create task for critical
    assert "note" in body  # summary note for non-critical

    # Skip when nothing found
    assert "skip" in body


def test_ensure_default_playbooks_idempotent_dependency_audit(tmp_path):
    """Calling ensure_default_playbooks twice does not overwrite dependency-audit.md."""
    ensure_default_playbooks(str(tmp_path))

    playbook_path = tmp_path / "vault" / "system" / "playbooks" / "dependency-audit.md"
    custom_content = "---\nid: dependency-audit\ntriggers:\n  - timer.24h\nscope: system\n---\n"
    playbook_path.write_text(custom_content)

    result = ensure_default_playbooks(str(tmp_path))

    assert playbook_path.read_text() == custom_content
    assert "dependency-audit.md" in result["skipped"]


def test_ensure_vault_layout_installs_default_playbooks(tmp_path):
    """ensure_vault_layout installs default playbooks as part of the layout."""
    ensure_vault_layout(str(tmp_path))

    assert (tmp_path / "vault" / "system" / "playbooks" / "task-outcome.md").is_file()
    assert (tmp_path / "vault" / "system" / "playbooks" / "codebase-inspector.md").is_file()
    assert (tmp_path / "vault" / "system" / "playbooks" / "dependency-audit.md").is_file()


def test_ensure_vault_layout_preserves_custom_playbooks(tmp_path):
    """ensure_vault_layout does not overwrite user-customised playbooks."""
    ensure_vault_layout(str(tmp_path))

    playbook_path = tmp_path / "vault" / "system" / "playbooks" / "task-outcome.md"
    custom = "# User's custom task-outcome playbook\n"
    playbook_path.write_text(custom)

    ensure_vault_layout(str(tmp_path))
    assert playbook_path.read_text() == custom


# ---------------------------------------------------------------------------
# Orchestrator profile tests (roadmap §4.2.3)
# ---------------------------------------------------------------------------


def test_ensure_orchestrator_profile_creates_file(tmp_path):
    """ensure_orchestrator_profile writes profile.md to vault/orchestrator/."""
    result = ensure_orchestrator_profile(str(tmp_path))

    assert result is True
    profile_path = tmp_path / "vault" / "orchestrator" / "profile.md"
    assert profile_path.is_file()
    assert profile_path.read_text() == ORCHESTRATOR_PROFILE


def test_ensure_orchestrator_profile_idempotent(tmp_path):
    """Calling ensure_orchestrator_profile twice does not overwrite the file."""
    ensure_orchestrator_profile(str(tmp_path))

    # Customise the file
    profile_path = tmp_path / "vault" / "orchestrator" / "profile.md"
    custom_content = "# My Custom Orchestrator Profile\n"
    profile_path.write_text(custom_content)

    result = ensure_orchestrator_profile(str(tmp_path))

    assert result is False
    assert profile_path.read_text() == custom_content


def test_ensure_orchestrator_profile_creates_parent_dirs(tmp_path):
    """Parent directories are created if they don't exist yet."""
    # Do NOT pre-create vault/orchestrator/
    assert not (tmp_path / "vault").exists()

    result = ensure_orchestrator_profile(str(tmp_path))

    assert result is True
    assert (tmp_path / "vault" / "orchestrator" / "profile.md").is_file()


def test_ensure_vault_layout_creates_orchestrator_profile(tmp_path):
    """ensure_vault_layout creates the orchestrator profile as part of startup."""
    ensure_vault_layout(str(tmp_path))

    profile_path = tmp_path / "vault" / "orchestrator" / "profile.md"
    assert profile_path.is_file()
    assert profile_path.read_text() == ORCHESTRATOR_PROFILE


def test_ensure_vault_layout_preserves_custom_orchestrator_profile(tmp_path):
    """ensure_vault_layout does not overwrite a user-customised orchestrator profile."""
    # First call creates default
    ensure_vault_layout(str(tmp_path))

    # User customises the profile
    profile_path = tmp_path / "vault" / "orchestrator" / "profile.md"
    custom = "# Custom orchestrator\n"
    profile_path.write_text(custom)

    # Second call should not overwrite
    ensure_vault_layout(str(tmp_path))
    assert profile_path.read_text() == custom


def test_orchestrator_profile_parses_without_errors():
    """The orchestrator profile template parses without errors."""
    from src.profile_parser import parse_profile

    result = parse_profile(ORCHESTRATOR_PROFILE)
    assert result.is_valid, f"Orchestrator profile has parse errors: {result.errors}"
    assert result.frontmatter.id == "orchestrator"
    assert result.frontmatter.name == "Orchestrator"


def test_orchestrator_profile_has_all_sections():
    """The orchestrator profile includes all 7 documented sections."""
    from src.profile_parser import parse_profile

    result = parse_profile(ORCHESTRATOR_PROFILE)
    assert result.is_valid

    # Structured sections
    assert "config" in result.sections
    assert "tools" in result.sections
    assert "mcp servers" in result.sections
    assert "install" in result.sections

    # Prompt sections
    assert "role" in result.sections
    assert "rules" in result.sections
    assert "reflection" in result.sections


def test_orchestrator_profile_frontmatter():
    """The orchestrator profile has correct frontmatter fields."""
    from src.profile_parser import parse_profile

    result = parse_profile(ORCHESTRATOR_PROFILE)
    assert result.frontmatter.id == "orchestrator"
    assert result.frontmatter.name == "Orchestrator"
    assert "profile" in result.frontmatter.tags
    assert "orchestrator" in result.frontmatter.tags


def test_orchestrator_profile_config():
    """The orchestrator profile Config block has valid model and permission_mode."""
    from src.profile_parser import parse_profile

    result = parse_profile(ORCHESTRATOR_PROFILE)
    assert result.config is not None
    assert "model" in result.config
    assert "permission_mode" in result.config


def test_orchestrator_profile_tools_deny_code_editing():
    """The orchestrator profile denies direct code-editing tools."""
    from src.profile_parser import parse_profile

    result = parse_profile(ORCHESTRATOR_PROFILE)
    assert result.tools is not None
    denied = result.tools.get("denied", [])
    # Orchestrator should not have access to direct code editing
    assert "file_write" in denied
    assert "file_edit" in denied
    assert "shell" in denied


def test_orchestrator_profile_role_mentions_coordination():
    """The orchestrator Role section describes coordination responsibilities."""
    from src.profile_parser import parse_profile

    result = parse_profile(ORCHESTRATOR_PROFILE)
    assert result.role is not None
    role_lower = result.role.lower()
    assert "orchestrator" in role_lower
    assert "delegate" in role_lower


def test_orchestrator_profile_syncs_to_agent_profile():
    """The orchestrator profile converts to a valid AgentProfile dict."""
    from src.profile_parser import parse_profile, parsed_profile_to_agent_profile

    result = parse_profile(ORCHESTRATOR_PROFILE)
    assert result.is_valid

    profile_dict = parsed_profile_to_agent_profile(result)
    assert profile_dict["id"] == "orchestrator"
    assert profile_dict["name"] == "Orchestrator"
    assert isinstance(profile_dict.get("system_prompt_suffix", ""), str)
    assert len(profile_dict.get("system_prompt_suffix", "")) > 0


# ---------------------------------------------------------------------------
# Starter knowledge pack copy tests (roadmap §4.3.4)
# ---------------------------------------------------------------------------


def test_copy_starter_knowledge_copies_matching_templates(tmp_path):
    """copy_starter_knowledge copies templates from knowledge/{type}/ to memory/."""
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    result = copy_starter_knowledge(str(tmp_path), "coding")

    assert "common-pitfalls.md" in result["copied"]
    assert "git-conventions.md" in result["copied"]
    assert result["skipped"] == []

    # Verify files exist in the memory directory
    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
    assert (memory_dir / "common-pitfalls.md").is_file()
    assert (memory_dir / "git-conventions.md").is_file()


def test_copy_starter_knowledge_file_contents_match(tmp_path):
    """Copied files have the same content as the templates."""
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    copy_starter_knowledge(str(tmp_path), "coding")

    templates_dir = tmp_path / "vault" / "templates" / "knowledge" / "coding"
    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"

    for filename in ("common-pitfalls.md", "git-conventions.md"):
        src_content = (templates_dir / filename).read_text()
        dst_content = (memory_dir / filename).read_text()
        assert src_content == dst_content


def test_copy_starter_knowledge_idempotent(tmp_path):
    """Calling copy_starter_knowledge twice doesn't overwrite existing files."""
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    result1 = copy_starter_knowledge(str(tmp_path), "coding")
    assert len(result1["copied"]) == 2

    # Modify a file to verify it's preserved
    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
    (memory_dir / "common-pitfalls.md").write_text("custom content")

    result2 = copy_starter_knowledge(str(tmp_path), "coding")
    assert result2["copied"] == []
    assert sorted(result2["skipped"]) == ["common-pitfalls.md", "git-conventions.md"]

    # Verify custom content was preserved
    assert (memory_dir / "common-pitfalls.md").read_text() == "custom content"


def test_copy_starter_knowledge_no_matching_templates(tmp_path):
    """No-op when no starter knowledge pack exists for the profile type."""
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "unknown-type")

    result = copy_starter_knowledge(str(tmp_path), "unknown-type")

    assert result["copied"] == []
    assert result["skipped"] == []


def test_copy_starter_knowledge_creates_memory_dir(tmp_path):
    """Memory directory is created if it doesn't exist."""
    ensure_default_templates(str(tmp_path))
    # Don't call ensure_vault_profile_dirs — memory dir doesn't exist yet

    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
    assert not memory_dir.exists()

    result = copy_starter_knowledge(str(tmp_path), "coding")
    assert len(result["copied"]) == 2
    assert memory_dir.is_dir()


def test_copy_starter_knowledge_code_review_type(tmp_path):
    """Copies code-review starter knowledge (review-checklist.md)."""
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "code-review")

    result = copy_starter_knowledge(str(tmp_path), "code-review")

    assert "review-checklist.md" in result["copied"]
    memory_dir = tmp_path / "vault" / "agent-types" / "code-review" / "memory"
    assert (memory_dir / "review-checklist.md").is_file()


def test_copy_starter_knowledge_qa_type(tmp_path):
    """Copies qa starter knowledge (testing-patterns.md)."""
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "qa")

    result = copy_starter_knowledge(str(tmp_path), "qa")

    assert "testing-patterns.md" in result["copied"]
    memory_dir = tmp_path / "vault" / "agent-types" / "qa" / "memory"
    assert (memory_dir / "testing-patterns.md").is_file()


def test_copy_starter_knowledge_preserves_starter_tag(tmp_path):
    """Copied files retain the #starter tag in frontmatter."""
    import yaml

    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    copy_starter_knowledge(str(tmp_path), "coding")

    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
    for filename in ("common-pitfalls.md", "git-conventions.md"):
        content = (memory_dir / filename).read_text()
        assert content.startswith("---")
        lines = content.strip().splitlines()
        end_idx = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
        fm = yaml.safe_load("\n".join(lines[1:end_idx]))
        assert "starter" in fm["tags"], f"{filename} missing 'starter' tag after copy"


def test_copy_starter_knowledge_partial_existing(tmp_path):
    """Only missing files are copied when some already exist."""
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    # Pre-create one of the files
    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
    (memory_dir / "common-pitfalls.md").write_text("already here")

    result = copy_starter_knowledge(str(tmp_path), "coding")

    assert result["copied"] == ["git-conventions.md"]
    assert result["skipped"] == ["common-pitfalls.md"]
    # Pre-existing file untouched
    assert (memory_dir / "common-pitfalls.md").read_text() == "already here"


def test_copy_starter_knowledge_skips_subdirectories(tmp_path):
    """Subdirectories inside templates/knowledge/{type}/ are not copied."""
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    # Create a subdirectory in the template dir
    subdir = tmp_path / "vault" / "templates" / "knowledge" / "coding" / "nested"
    subdir.mkdir()
    (subdir / "deep-file.md").write_text("deep")

    result = copy_starter_knowledge(str(tmp_path), "coding")

    # Only top-level files are copied
    assert "deep-file.md" not in result["copied"]
    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
    assert not (memory_dir / "deep-file.md").exists()


def test_copy_starter_knowledge_no_templates_dir(tmp_path):
    """Returns empty result when templates dir doesn't exist at all."""
    # Don't call ensure_default_templates — no templates dir at all
    result = copy_starter_knowledge(str(tmp_path), "coding")

    assert result["copied"] == []
    assert result["skipped"] == []


def test_copy_starter_knowledge_all_types(tmp_path):
    """All agent types in _STARTER_KNOWLEDGE can be copied successfully."""
    ensure_default_templates(str(tmp_path))

    for agent_type, files in _STARTER_KNOWLEDGE.items():
        ensure_vault_profile_dirs(str(tmp_path), agent_type)
        result = copy_starter_knowledge(str(tmp_path), agent_type)

        assert sorted(result["copied"]) == sorted(files.keys()), (
            f"Mismatch for {agent_type}: expected {sorted(files.keys())}, "
            f"got {sorted(result['copied'])}"
        )


# ---------------------------------------------------------------------------
# Starter knowledge pack provisioning — Roadmap §4.3.5 supplementary tests
# ---------------------------------------------------------------------------


def test_starter_files_are_copies_not_symlinks(tmp_path):
    """(g) Starter files are copies (not symlinks) — editing them doesn't affect template.

    Verifies that copy_starter_knowledge produces independent file copies.
    Modifying a copied file must not alter the source template.
    """
    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    copy_starter_knowledge(str(tmp_path), "coding")

    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"
    template_dir = tmp_path / "vault" / "templates" / "knowledge" / "coding"

    for filename in ("common-pitfalls.md", "git-conventions.md"):
        copied = memory_dir / filename
        template = template_dir / filename

        # Must not be a symlink
        assert not copied.is_symlink(), f"{filename} in memory/ should not be a symlink"

        # Read original template
        original = template.read_text()

        # Modify the copy
        copied.write_text("completely replaced content")

        # Template must be untouched
        assert template.read_text() == original, (
            f"Modifying memory/{filename} must not change templates/knowledge/coding/{filename}"
        )


def test_starter_tag_discoverable_across_all_types(tmp_path):
    """(f) #starter tag allows users to identify and optionally remove starter content.

    Every starter knowledge file across all agent types must be discoverable
    by searching for the 'starter' tag in YAML frontmatter.
    """
    import yaml

    ensure_default_templates(str(tmp_path))

    discovered: list[str] = []

    for agent_type, expected_files in _STARTER_KNOWLEDGE.items():
        ensure_vault_profile_dirs(str(tmp_path), agent_type)
        copy_starter_knowledge(str(tmp_path), agent_type)

        memory_dir = tmp_path / "vault" / "agent-types" / agent_type / "memory"

        for filename in sorted(expected_files):
            filepath = memory_dir / filename
            assert filepath.is_file(), f"Expected {agent_type}/{filename} to exist"

            content = filepath.read_text()
            assert content.startswith("---"), f"{agent_type}/{filename} has no YAML frontmatter"

            lines = content.strip().splitlines()
            end_idx = next(i for i, line in enumerate(lines[1:], 1) if line == "---")
            fm = yaml.safe_load("\n".join(lines[1:end_idx]))

            assert fm is not None, f"{agent_type}/{filename} frontmatter parsed as None"
            tags = fm.get("tags", [])
            assert "starter" in tags, (
                f"{agent_type}/{filename} must have 'starter' tag for discoverability, "
                f"got tags={tags}"
            )
            discovered.append(f"{agent_type}/{filename}")

    # Sanity: we found tags in every expected file
    total_expected = sum(len(f) for f in _STARTER_KNOWLEDGE.values())
    assert len(discovered) == total_expected


def test_starter_tag_removable_by_user(tmp_path):
    """(f) Users can optionally remove starter content identified by #starter tag.

    Verifies the user workflow: find files by tag, then delete them.
    After removal, the files are gone and re-running copy won't restore them
    (the memory directory already has other files, but the deleted ones are
    re-copied since they no longer exist — by design, allowing re-provisioning).
    """
    import yaml

    ensure_default_templates(str(tmp_path))
    ensure_vault_profile_dirs(str(tmp_path), "coding")
    copy_starter_knowledge(str(tmp_path), "coding")

    memory_dir = tmp_path / "vault" / "agent-types" / "coding" / "memory"

    # Step 1: User identifies starter files by #starter tag
    starter_files = []
    for md_file in memory_dir.glob("*.md"):
        content = md_file.read_text()
        if content.startswith("---"):
            lines = content.strip().splitlines()
            end_idx = next(
                (i for i, line in enumerate(lines[1:], 1) if line == "---"),
                None,
            )
            if end_idx:
                fm = yaml.safe_load("\n".join(lines[1:end_idx]))
                if fm and "starter" in fm.get("tags", []):
                    starter_files.append(md_file)

    assert len(starter_files) == 2, "Should find 2 starter files for coding type"

    # Step 2: User removes a starter file
    starter_files[0].unlink()
    remaining = list(memory_dir.glob("*.md"))
    assert len(remaining) == 1, "One starter file should remain after deletion"

    # Step 3: Re-running copy restores only the deleted file (idempotent)
    result = copy_starter_knowledge(str(tmp_path), "coding")
    assert len(result["copied"]) == 1, "Only the deleted file should be re-copied"
    assert len(result["skipped"]) == 1, "The remaining file should be skipped"
