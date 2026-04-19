"""Tests for the consolidated vault migration (``run_vault_migration``, spec §6).

Covers:
- Project discovery from filesystem
- Dry-run mode (scan without changes)
- Live mode (execute all migrations)
- Idempotency (safe to run multiple times)
- Correct operation ordering
- Report structure and summary accuracy
- Edge cases (empty data dir, partial data, etc.)
- Startup auto-migration helpers (has_legacy_data, vault_has_content)
"""

from __future__ import annotations

import os
import time

from src.vault import (
    _discover_project_ids,
    _scan_memory_copy,
    _scan_notes_migration,
    _scan_obsidian_migration,
    _scan_passive_rule_migration,
    _scan_rule_migration,
    has_legacy_data,
    migrate_passive_rules_to_memory,
    run_vault_migration,
    vault_has_content,
)


# ---------------------------------------------------------------------------
# _discover_project_ids
# ---------------------------------------------------------------------------


def test_discover_projects_from_notes(tmp_path):
    """Projects are discovered from notes/ subdirectories."""
    (tmp_path / "notes" / "proj-a").mkdir(parents=True)
    (tmp_path / "notes" / "proj-b").mkdir(parents=True)

    result = _discover_project_ids(str(tmp_path))
    assert result == ["proj-a", "proj-b"]


def test_discover_projects_from_memory(tmp_path):
    """Projects are discovered from memory/ subdirectories (excluding global)."""
    (tmp_path / "memory" / "proj-x").mkdir(parents=True)
    (tmp_path / "memory" / "global").mkdir(parents=True)

    result = _discover_project_ids(str(tmp_path))
    assert result == ["proj-x"]
    assert "global" not in result


def test_discover_projects_deduplicates(tmp_path):
    """Projects appearing in both notes/ and memory/ are deduplicated."""
    (tmp_path / "notes" / "shared-proj").mkdir(parents=True)
    (tmp_path / "memory" / "shared-proj").mkdir(parents=True)
    (tmp_path / "memory" / "mem-only").mkdir(parents=True)

    result = _discover_project_ids(str(tmp_path))
    assert result == ["mem-only", "shared-proj"]


def test_discover_projects_empty_dir(tmp_path):
    """Empty data directory returns empty list."""
    result = _discover_project_ids(str(tmp_path))
    assert result == []


def test_discover_projects_ignores_hidden_dirs(tmp_path):
    """Hidden directories in memory/ (like .obsidian) are excluded."""
    (tmp_path / "memory" / ".obsidian").mkdir(parents=True)
    (tmp_path / "memory" / ".hidden").mkdir(parents=True)
    (tmp_path / "memory" / "real-project").mkdir(parents=True)

    result = _discover_project_ids(str(tmp_path))
    assert result == ["real-project"]


# ---------------------------------------------------------------------------
# Dry-run scan helpers
# ---------------------------------------------------------------------------


def test_scan_obsidian_would_move(tmp_path):
    """Scan reports 'move' when source exists and dest does not."""
    (tmp_path / "memory" / ".obsidian").mkdir(parents=True)

    result = _scan_obsidian_migration(str(tmp_path))
    assert result["action"] == "move"


def test_scan_obsidian_skip_no_source(tmp_path):
    """Scan reports 'skip' when source does not exist."""
    result = _scan_obsidian_migration(str(tmp_path))
    assert result["action"] == "skip"
    assert "source" in result["reason"]


def test_scan_obsidian_skip_dest_exists(tmp_path):
    """Scan reports 'skip' when destination already exists."""
    (tmp_path / "memory" / ".obsidian").mkdir(parents=True)
    (tmp_path / "vault" / ".obsidian").mkdir(parents=True)

    result = _scan_obsidian_migration(str(tmp_path))
    assert result["action"] == "skip"
    assert "destination" in result["reason"]


def test_scan_notes_reports_files(tmp_path):
    """Notes scan reports files that would be moved and skipped."""
    source = tmp_path / "notes" / "proj"
    source.mkdir(parents=True)
    (source / "new-note.md").write_text("content")
    (source / "existing.md").write_text("old")

    dest = tmp_path / "vault" / "projects" / "proj" / "notes"
    dest.mkdir(parents=True)
    (dest / "existing.md").write_text("already there")

    result = _scan_notes_migration(str(tmp_path), "proj")
    assert result["would_move"] == 1
    assert result["would_skip"] == 1
    assert len(result["files"]) == 2


def test_scan_notes_empty_source(tmp_path):
    """Notes scan returns zeros when source does not exist."""
    result = _scan_notes_migration(str(tmp_path), "nonexistent")
    assert result["would_move"] == 0
    assert result["would_skip"] == 0


def test_scan_memory_reports_files(tmp_path):
    """Memory scan reports files that would be copied, updated, and skipped."""
    source = tmp_path / "memory" / "proj"
    source.mkdir(parents=True)
    (source / "profile.md").write_text("new profile")
    (source / "factsheet.md").write_text("factsheet")

    dest = tmp_path / "vault" / "projects" / "proj" / "memory"
    dest.mkdir(parents=True)

    # factsheet.md exists and is newer than source
    (dest / "factsheet.md").write_text("already copied")
    now = time.time()
    os.utime(str(source / "factsheet.md"), (now - 10, now - 10))
    os.utime(str(dest / "factsheet.md"), (now, now))

    result = _scan_memory_copy(str(tmp_path), "proj")
    assert result["would_copy"] == 1  # profile.md
    assert result["would_skip"] == 1  # factsheet.md
    assert len(result["files"]) == 2


def test_scan_memory_detects_updates(tmp_path):
    """Memory scan detects when source is newer than destination."""
    source = tmp_path / "memory" / "proj"
    source.mkdir(parents=True)

    dest = tmp_path / "vault" / "projects" / "proj" / "memory"
    dest.mkdir(parents=True)

    # Write dest first with old timestamp
    (dest / "profile.md").write_text("stale")
    now = time.time()
    os.utime(str(dest / "profile.md"), (now - 10, now - 10))

    # Source with newer timestamp
    (source / "profile.md").write_text("updated")
    os.utime(str(source / "profile.md"), (now, now))

    result = _scan_memory_copy(str(tmp_path), "proj")
    assert result["would_update"] == 1


def test_scan_rules_reports_files(tmp_path):
    """Rule scan reports files that would be moved and skipped."""
    rules_dir = tmp_path / "memory" / "global" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "rule-new.md").write_text("# New rule")
    (rules_dir / "rule-existing.md").write_text("# Existing rule")

    dest = tmp_path / "vault" / "system" / "playbooks"
    dest.mkdir(parents=True)
    (dest / "rule-existing.md").write_text("# Already migrated")

    result = _scan_rule_migration(str(tmp_path))
    assert result["would_move"] == 1
    assert result["would_skip"] == 1


# ---------------------------------------------------------------------------
# run_vault_migration — dry-run mode
# ---------------------------------------------------------------------------


def test_dry_run_reports_without_changes(tmp_path):
    """Dry-run mode scans and reports without modifying files."""
    # Set up legacy data
    (tmp_path / "memory" / ".obsidian").mkdir(parents=True)
    (tmp_path / "memory" / ".obsidian" / "app.json").write_text("{}")

    source_notes = tmp_path / "notes" / "my-proj"
    source_notes.mkdir(parents=True)
    (source_notes / "design.md").write_text("# Design")

    source_mem = tmp_path / "memory" / "my-proj"
    source_mem.mkdir(parents=True)
    (source_mem / "profile.md").write_text("# Profile")

    rules_dir = tmp_path / "memory" / "global" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "rule-a.md").write_text("# Rule A")

    report = run_vault_migration(str(tmp_path), dry_run=True)

    assert report["dry_run"] is True
    assert "my-proj" in report["projects_discovered"]

    # Verify nothing was actually moved
    assert (tmp_path / "memory" / ".obsidian" / "app.json").exists()
    assert (source_notes / "design.md").exists()
    assert (source_mem / "profile.md").exists()
    assert (rules_dir / "rule-a.md").exists()

    # Verify vault was NOT created
    assert not (tmp_path / "vault" / ".obsidian").exists()

    # Summary should have positive counts
    s = report["summary"]
    assert s["total_moved"] > 0 or s["total_copied"] > 0


def test_dry_run_with_explicit_projects(tmp_path):
    """Dry-run with explicit project_ids does not auto-discover."""
    (tmp_path / "notes" / "proj-a").mkdir(parents=True)
    (tmp_path / "notes" / "proj-a" / "note.md").write_text("content")
    (tmp_path / "notes" / "proj-b").mkdir(parents=True)
    (tmp_path / "notes" / "proj-b" / "note.md").write_text("content")

    report = run_vault_migration(str(tmp_path), project_ids=["proj-a"], dry_run=True)

    assert report["projects_discovered"] == ["proj-a"]
    assert "proj-a" in report["notes"]
    assert "proj-b" not in report["notes"]


# ---------------------------------------------------------------------------
# run_vault_migration — live mode
# ---------------------------------------------------------------------------


def test_live_migration_full_scenario(tmp_path):
    """Full migration: obsidian, notes, memory, and rules all migrated."""
    # Obsidian config
    obs_src = tmp_path / "memory" / ".obsidian"
    obs_src.mkdir(parents=True)
    (obs_src / "app.json").write_text('{"theme": "dark"}')

    # Notes for two projects
    for pid in ("proj-a", "proj-b"):
        notes = tmp_path / "notes" / pid
        notes.mkdir(parents=True)
        (notes / "todo.md").write_text(f"# TODOs for {pid}")

    # Memory for one project
    mem = tmp_path / "memory" / "proj-a"
    mem.mkdir(parents=True)
    (mem / "profile.md").write_text("# Profile A")
    (mem / "factsheet.md").write_text("# Facts A")

    # Global rules
    rules = tmp_path / "memory" / "global" / "rules"
    rules.mkdir(parents=True)
    (rules / "rule-test.md").write_text("# Rule")

    report = run_vault_migration(str(tmp_path))

    assert report["dry_run"] is False

    # Obsidian moved
    assert (tmp_path / "vault" / ".obsidian" / "app.json").exists()
    assert not obs_src.exists()

    # Notes moved
    for pid in ("proj-a", "proj-b"):
        assert (tmp_path / "vault" / "projects" / pid / "notes" / "todo.md").exists()

    # Memory copied (source preserved)
    dest_mem = tmp_path / "vault" / "projects" / "proj-a" / "memory"
    assert (dest_mem / "profile.md").read_text() == "# Profile A"
    assert (mem / "profile.md").exists()  # source still there

    # Rules moved
    assert (tmp_path / "vault" / "system" / "playbooks" / "rule-test.md").exists()
    assert not (rules / "rule-test.md").exists()

    # Summary
    s = report["summary"]
    assert s["total_errors"] == 0
    assert s["total_moved"] > 0
    assert s["total_copied"] > 0


def test_live_migration_creates_vault_structure(tmp_path):
    """Live migration creates the vault directory structure."""
    report = run_vault_migration(str(tmp_path))

    # system/supervisor scopes were merged: supervisor now holds the
    # playbooks and memory dirs; system/ remains as a stub.
    assert (tmp_path / "vault" / "system").is_dir()
    assert (tmp_path / "vault" / "agent-types" / "supervisor" / "playbooks").is_dir()
    assert (tmp_path / "vault" / "agent-types" / "supervisor" / "memory").is_dir()
    assert (tmp_path / "vault" / "templates").is_dir()
    assert report["summary"]["total_errors"] == 0


def test_live_migration_creates_project_dirs(tmp_path):
    """Live migration creates per-project vault directories."""
    (tmp_path / "notes" / "my-proj").mkdir(parents=True)
    (tmp_path / "notes" / "my-proj" / "note.md").write_text("x")

    run_vault_migration(str(tmp_path))

    base = tmp_path / "vault" / "projects" / "my-proj"
    assert (base / "notes").is_dir()
    assert (base / "memory" / "knowledge").is_dir()
    assert (base / "playbooks").is_dir()
    assert (base / "references").is_dir()
    assert (base / "overrides").is_dir()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_double_run(tmp_path):
    """Running migration twice produces same result — second run is no-op."""
    # Set up data
    obs = tmp_path / "memory" / ".obsidian"
    obs.mkdir(parents=True)
    (obs / "app.json").write_text("{}")

    notes = tmp_path / "notes" / "proj"
    notes.mkdir(parents=True)
    (notes / "note.md").write_text("content")

    mem = tmp_path / "memory" / "proj"
    mem.mkdir(parents=True)
    (mem / "profile.md").write_text("# Profile")

    rules = tmp_path / "memory" / "global" / "rules"
    rules.mkdir(parents=True)
    (rules / "rule.md").write_text("# Rule")

    # First run: should migrate everything
    report1 = run_vault_migration(str(tmp_path))
    s1 = report1["summary"]
    assert s1["total_moved"] > 0 or s1["total_copied"] > 0

    # Second run: should be a no-op
    report2 = run_vault_migration(str(tmp_path))
    s2 = report2["summary"]
    assert s2["total_moved"] == 0
    # Memory copy might still report 0 if mtimes match
    assert s2["total_errors"] == 0

    # Verify data integrity
    assert (tmp_path / "vault" / ".obsidian" / "app.json").read_text() == "{}"
    assert (tmp_path / "vault" / "projects" / "proj" / "notes" / "note.md").read_text() == "content"


def test_idempotent_no_data(tmp_path):
    """Running migration on empty data dir is a safe no-op."""
    report = run_vault_migration(str(tmp_path))

    assert report["summary"]["total_moved"] == 0
    assert report["summary"]["total_copied"] == 0
    assert report["summary"]["total_errors"] == 0
    assert report["projects_discovered"] == []


# ---------------------------------------------------------------------------
# Report structure
# ---------------------------------------------------------------------------


def test_report_has_required_keys(tmp_path):
    """Report dict contains all expected top-level keys."""
    report = run_vault_migration(str(tmp_path))

    assert "dry_run" in report
    assert "data_dir" in report
    assert "projects_discovered" in report
    assert "obsidian" in report
    assert "notes" in report
    assert "memory" in report
    assert "rules" in report
    assert "summary" in report
    assert "details" in report


def test_report_summary_keys(tmp_path):
    """Summary sub-dict has all expected count keys."""
    report = run_vault_migration(str(tmp_path))
    s = report["summary"]

    assert "total_moved" in s
    assert "total_copied" in s
    assert "total_skipped" in s
    assert "total_errors" in s


def test_report_details_are_strings(tmp_path):
    """Details list contains only string entries."""
    (tmp_path / "notes" / "proj").mkdir(parents=True)
    (tmp_path / "notes" / "proj" / "note.md").write_text("x")

    report = run_vault_migration(str(tmp_path), dry_run=True)

    assert isinstance(report["details"], list)
    for entry in report["details"]:
        assert isinstance(entry, str)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_migration_with_only_obsidian(tmp_path):
    """Migration works when only obsidian config exists (no projects)."""
    obs = tmp_path / "memory" / ".obsidian"
    obs.mkdir(parents=True)
    (obs / "themes.json").write_text("{}")

    report = run_vault_migration(str(tmp_path))

    assert report["obsidian"]["action"] == "moved"
    assert (tmp_path / "vault" / ".obsidian" / "themes.json").exists()
    assert report["summary"]["total_errors"] == 0


def test_migration_with_only_notes(tmp_path):
    """Migration works when only notes exist."""
    notes = tmp_path / "notes" / "my-proj"
    notes.mkdir(parents=True)
    (notes / "readme.md").write_text("# Notes")

    report = run_vault_migration(str(tmp_path))

    assert "my-proj" in report["projects_discovered"]
    assert (tmp_path / "vault" / "projects" / "my-proj" / "notes" / "readme.md").exists()


def test_migration_with_only_rules(tmp_path):
    """Migration works when only rule files exist."""
    rules = tmp_path / "memory" / "global" / "rules"
    rules.mkdir(parents=True)
    (rules / "rule-only.md").write_text("# Solo rule")

    report = run_vault_migration(str(tmp_path))

    assert (tmp_path / "vault" / "system" / "playbooks" / "rule-only.md").exists()
    assert report["rules"]["moved"] == 1


def test_migration_explicit_project_ids(tmp_path):
    """Explicit project_ids override auto-discovery."""
    # Create notes for two projects
    for pid in ("proj-a", "proj-b"):
        notes = tmp_path / "notes" / pid
        notes.mkdir(parents=True)
        (notes / "note.md").write_text(f"note for {pid}")

    # Only migrate proj-a
    report = run_vault_migration(str(tmp_path), project_ids=["proj-a"])

    assert report["projects_discovered"] == ["proj-a"]
    assert (tmp_path / "vault" / "projects" / "proj-a" / "notes" / "note.md").exists()
    # proj-b notes should NOT be moved
    assert (tmp_path / "notes" / "proj-b" / "note.md").exists()


def test_migration_handles_project_rules(tmp_path):
    """Per-project rules are migrated alongside global rules."""
    # Global rule
    global_rules = tmp_path / "memory" / "global" / "rules"
    global_rules.mkdir(parents=True)
    (global_rules / "rule-g.md").write_text("# Global")

    # Project rule
    proj_rules = tmp_path / "memory" / "proj-x" / "rules"
    proj_rules.mkdir(parents=True)
    (proj_rules / "rule-p.md").write_text("# Project")

    report = run_vault_migration(str(tmp_path))

    assert (tmp_path / "vault" / "system" / "playbooks" / "rule-g.md").exists()
    assert (tmp_path / "vault" / "projects" / "proj-x" / "playbooks" / "rule-p.md").exists()
    assert report["rules"]["moved"] == 2
    assert report["rules"]["errors"] == 0


# ---------------------------------------------------------------------------
# has_legacy_data
# ---------------------------------------------------------------------------


def test_has_legacy_data_empty_dir(tmp_path):
    """Empty data directory has no legacy data."""
    assert has_legacy_data(str(tmp_path)) is False


def test_has_legacy_data_notes_with_files(tmp_path):
    """Notes directory with project subdirs containing files is legacy data."""
    notes = tmp_path / "notes" / "my-proj"
    notes.mkdir(parents=True)
    (notes / "design.md").write_text("# Design")

    assert has_legacy_data(str(tmp_path)) is True


def test_has_legacy_data_notes_empty_subdir(tmp_path):
    """Notes directory with empty project subdirs is NOT legacy data."""
    (tmp_path / "notes" / "empty-proj").mkdir(parents=True)

    assert has_legacy_data(str(tmp_path)) is False


def test_has_legacy_data_memory_rules(tmp_path):
    """Memory directory with rules/*.md files is legacy data."""
    rules = tmp_path / "memory" / "global" / "rules"
    rules.mkdir(parents=True)
    (rules / "rule.md").write_text("# Rule")

    assert has_legacy_data(str(tmp_path)) is True


def test_has_legacy_data_memory_rules_no_md(tmp_path):
    """Memory rules directory with non-.md files only is NOT legacy data."""
    rules = tmp_path / "memory" / "global" / "rules"
    rules.mkdir(parents=True)
    (rules / "readme.txt").write_text("not a rule")

    assert has_legacy_data(str(tmp_path)) is False


def test_has_legacy_data_obsidian_config(tmp_path):
    """Obsidian config at memory/.obsidian/ is legacy data."""
    (tmp_path / "memory" / ".obsidian").mkdir(parents=True)

    assert has_legacy_data(str(tmp_path)) is True


def test_has_legacy_data_project_memory_profile(tmp_path):
    """Project memory with profile.md is legacy data."""
    mem = tmp_path / "memory" / "my-proj"
    mem.mkdir(parents=True)
    (mem / "profile.md").write_text("# Profile")

    assert has_legacy_data(str(tmp_path)) is True


def test_has_legacy_data_project_memory_factsheet(tmp_path):
    """Project memory with factsheet.md is legacy data."""
    mem = tmp_path / "memory" / "my-proj"
    mem.mkdir(parents=True)
    (mem / "factsheet.md").write_text("# Facts")

    assert has_legacy_data(str(tmp_path)) is True


def test_has_legacy_data_project_memory_knowledge(tmp_path):
    """Project memory with knowledge/ directory is legacy data."""
    (tmp_path / "memory" / "my-proj" / "knowledge").mkdir(parents=True)

    assert has_legacy_data(str(tmp_path)) is True


def test_has_legacy_data_global_memory_not_project(tmp_path):
    """Global memory dir (no rules, no .obsidian) is NOT legacy project data."""
    mem = tmp_path / "memory" / "global"
    mem.mkdir(parents=True)
    (mem / "some-file.md").write_text("global stuff")

    assert has_legacy_data(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# vault_has_content
# ---------------------------------------------------------------------------


def test_vault_has_content_no_vault(tmp_path):
    """No vault directory means no content."""
    assert vault_has_content(str(tmp_path)) is False


def test_vault_has_content_empty_skeleton(tmp_path):
    """Vault with only empty directories has no content."""
    for d in (
        "vault/system/playbooks",
        "vault/system/memory",
        "vault/orchestrator/playbooks",
        "vault/projects",
        "vault/templates",
    ):
        (tmp_path / d).mkdir(parents=True)

    assert vault_has_content(str(tmp_path)) is False


def test_vault_has_content_obsidian_only(tmp_path):
    """Vault with only .obsidian/ config is NOT considered to have content."""
    obs = tmp_path / "vault" / ".obsidian"
    obs.mkdir(parents=True)
    (obs / "app.json").write_text('{"theme": "dark"}')

    assert vault_has_content(str(tmp_path)) is False


def test_vault_has_content_with_playbook(tmp_path):
    """Vault with a playbook file has content."""
    playbooks = tmp_path / "vault" / "system" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "task-outcome.md").write_text("# Playbook")

    assert vault_has_content(str(tmp_path)) is True


def test_vault_has_content_with_project_notes(tmp_path):
    """Vault with project notes has content."""
    notes = tmp_path / "vault" / "projects" / "my-proj" / "notes"
    notes.mkdir(parents=True)
    (notes / "design.md").write_text("# Design")

    assert vault_has_content(str(tmp_path)) is True


def test_vault_has_content_with_memory_file(tmp_path):
    """Vault with project memory file has content."""
    mem = tmp_path / "vault" / "projects" / "my-proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "profile.md").write_text("# Profile")

    assert vault_has_content(str(tmp_path)) is True


# ------------------------------------------------------------------
# Passive rule migration (playbooks spec §13, Phase 2)
# ------------------------------------------------------------------


def _write_rule_file(path, rule_id, rule_type, project_id=None, content="# Rule"):
    """Helper to write a rule file with proper frontmatter."""
    import yaml

    meta = {
        "id": rule_id,
        "type": rule_type,
        "project_id": project_id,
        "hooks": [],
        "created": "2024-01-01T00:00:00+00:00",
        "updated": "2024-01-01T00:00:00+00:00",
    }
    frontmatter = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
    file_content = f"---\n{frontmatter}\n---\n\n{content}\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(file_content)


def test_migrate_passive_rules_moves_to_guidance(tmp_path):
    """Passive rules in playbooks/ are moved to memory/guidance/."""
    data_dir = str(tmp_path)

    # Create a passive rule in system playbooks
    system_playbooks = tmp_path / "vault" / "system" / "playbooks"
    system_playbooks.mkdir(parents=True)
    _write_rule_file(
        str(system_playbooks / "rule-style-guide.md"),
        "rule-style-guide",
        "passive",
        content="# Style Guide\n\nUse consistent formatting.",
    )

    # Create a passive rule in project playbooks
    proj_playbooks = tmp_path / "vault" / "projects" / "my-proj" / "playbooks"
    proj_playbooks.mkdir(parents=True)
    _write_rule_file(
        str(proj_playbooks / "rule-naming.md"),
        "rule-naming",
        "passive",
        project_id="my-proj",
        content="# Naming Conventions\n\nUse snake_case.",
    )

    # Create an active rule that should NOT be moved
    _write_rule_file(
        str(system_playbooks / "rule-monitor.md"),
        "rule-monitor",
        "active",
        content="# Monitor\n\n## Trigger\nEvery 5 minutes.",
    )

    result = migrate_passive_rules_to_memory(data_dir)

    assert result["moved"] == 2
    assert result["skipped"] == 0
    assert result["errors"] == 0

    # Passive rules should be at new locations
    assert (tmp_path / "vault" / "system" / "memory" / "guidance" / "rule-style-guide.md").is_file()
    assert (
        tmp_path / "vault" / "projects" / "my-proj" / "memory" / "guidance" / "rule-naming.md"
    ).is_file()

    # Source files should be removed
    assert not (system_playbooks / "rule-style-guide.md").exists()
    assert not (proj_playbooks / "rule-naming.md").exists()

    # Active rule should remain in playbooks
    assert (system_playbooks / "rule-monitor.md").is_file()


def test_migrate_passive_rules_idempotent(tmp_path):
    """Second run skips already-migrated passive rules."""
    data_dir = str(tmp_path)

    system_playbooks = tmp_path / "vault" / "system" / "playbooks"
    system_playbooks.mkdir(parents=True)
    _write_rule_file(
        str(system_playbooks / "rule-test.md"),
        "rule-test",
        "passive",
    )

    # First run
    result1 = migrate_passive_rules_to_memory(data_dir)
    assert result1["moved"] == 1

    # Second run — nothing to move
    result2 = migrate_passive_rules_to_memory(data_dir)
    assert result2["moved"] == 0
    assert result2["skipped"] == 0  # Source file was already removed


def test_migrate_passive_rules_updates_frontmatter(tmp_path):
    """Migrated files have tags added and hooks removed from frontmatter."""
    import yaml

    data_dir = str(tmp_path)

    system_playbooks = tmp_path / "vault" / "system" / "playbooks"
    system_playbooks.mkdir(parents=True)
    _write_rule_file(
        str(system_playbooks / "rule-guidance.md"),
        "rule-guidance",
        "passive",
        content="# Guidance\n\nSome guidance.",
    )

    migrate_passive_rules_to_memory(data_dir)

    dest = tmp_path / "vault" / "system" / "memory" / "guidance" / "rule-guidance.md"
    assert dest.is_file()

    raw = dest.read_text()
    parts = raw.split("---", 2)
    meta = yaml.safe_load(parts[1])

    assert "tags" in meta
    assert "guidance" in meta["tags"]
    assert "passive-rule" in meta["tags"]
    assert "hooks" not in meta  # hooks removed for memory files


def test_migrate_passive_rules_preserves_dest_if_exists(tmp_path):
    """If destination already exists, migration skips that file."""
    data_dir = str(tmp_path)

    system_playbooks = tmp_path / "vault" / "system" / "playbooks"
    system_playbooks.mkdir(parents=True)
    _write_rule_file(
        str(system_playbooks / "rule-existing.md"),
        "rule-existing",
        "passive",
    )

    # Pre-create destination
    dest_dir = tmp_path / "vault" / "system" / "memory" / "guidance"
    dest_dir.mkdir(parents=True)
    (dest_dir / "rule-existing.md").write_text("# Already here\n")

    result = migrate_passive_rules_to_memory(data_dir)
    assert result["moved"] == 0
    assert result["skipped"] == 1

    # Original should still exist (not deleted when skip)
    assert (system_playbooks / "rule-existing.md").is_file()

    # Destination should be unchanged
    assert (dest_dir / "rule-existing.md").read_text() == "# Already here\n"


def test_scan_passive_rule_migration_dry_run(tmp_path):
    """Dry-run scan reports what would be moved."""
    data_dir = str(tmp_path)

    system_playbooks = tmp_path / "vault" / "system" / "playbooks"
    system_playbooks.mkdir(parents=True)
    _write_rule_file(
        str(system_playbooks / "rule-passive.md"),
        "rule-passive",
        "passive",
    )
    _write_rule_file(
        str(system_playbooks / "rule-active.md"),
        "rule-active",
        "active",
    )

    scan = _scan_passive_rule_migration(data_dir)
    assert scan["would_move"] == 1
    assert scan["would_skip"] == 0
    assert len(scan["details"]) == 1
    assert "MOVE" in scan["details"][0]
    assert "rule-passive.md" in scan["details"][0]


def test_run_vault_migration_includes_passive_rules(tmp_path):
    """run_vault_migration includes passive rule migration in report."""
    data_dir = str(tmp_path)

    # Set up vault layout
    system_playbooks = tmp_path / "vault" / "system" / "playbooks"
    system_playbooks.mkdir(parents=True)
    (tmp_path / "vault" / "system" / "memory").mkdir(parents=True, exist_ok=True)

    _write_rule_file(
        str(system_playbooks / "rule-guidance.md"),
        "rule-guidance",
        "passive",
    )

    report = run_vault_migration(data_dir)

    # Check passive_rules section exists in report
    assert "passive_rules" in report
    assert report["passive_rules"]["moved"] == 1
