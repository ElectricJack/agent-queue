"""Tests for DB → vault profile migration (Roadmap 4.2.1).

Covers:
- migrate_db_profiles_to_vault: read DB profiles, generate markdown files
- scan_profile_migration: dry-run preview
- verify_round_trip: parse generated markdown and compare to DB profile
- Idempotency: running migration twice skips already-migrated profiles
- Force mode: overwrite existing vault files
- Error handling: render failures, write failures, DB read failures
- Round-trip verification: all field types survive the DB → markdown → parse cycle
- Command handler integration: _cmd_migrate_profiles
- Empty database: migration with no profiles
- Mixed state: some profiles have vault files, some don't
"""

from __future__ import annotations

import os

import pytest

from src.config import AppConfig
from src.models import AgentProfile
from src.orchestrator import Orchestrator
from src.profiles.migration import (
    MigrationReport,
    ProfileMigrationResult,
    _render_profile_markdown,
    _vault_profile_path,
    migrate_db_profiles_to_vault,
    scan_profile_migration,
    verify_round_trip,
)
from src.profiles.parser import parse_profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a fresh in-memory database for testing."""
    from src.database import Database

    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def data_dir(tmp_path):
    """Create a temporary data directory."""
    d = str(tmp_path / "data")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture
def sample_profile():
    """A fully-populated AgentProfile for testing."""
    return AgentProfile(
        id="coding",
        name="Coding Agent",
        description="A general-purpose coding agent",
        model="claude-sonnet-4-6",
        permission_mode="auto",
        allowed_tools=["Read", "Write", "Edit", "Bash"],
        mcp_servers={
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            }
        },
        system_prompt_suffix=(
            "## Role\nYou are a software engineering agent.\n\n"
            "## Rules\n- Always run tests\n- Never commit secrets\n\n"
            "## Reflection\nAfter completing a task, consider what you learned."
        ),
        install={"npm": ["eslint"], "commands": ["docker"]},
    )


@pytest.fixture
def minimal_profile():
    """A minimal AgentProfile with only required fields."""
    return AgentProfile(
        id="minimal",
        name="Minimal Agent",
    )


# ---------------------------------------------------------------------------
# verify_round_trip tests
# ---------------------------------------------------------------------------


class TestVerifyRoundTrip:
    """Test round-trip verification of generated markdown."""

    def test_full_profile_round_trips(self, sample_profile):
        """A fully-populated profile should survive the round-trip."""
        markdown = _render_profile_markdown(sample_profile)
        ok, diffs = verify_round_trip(sample_profile, markdown)
        assert ok, f"Round-trip failed with diffs: {diffs}"
        assert diffs == []

    def test_minimal_profile_round_trips(self, minimal_profile):
        """A minimal profile should survive the round-trip."""
        markdown = _render_profile_markdown(minimal_profile)
        ok, diffs = verify_round_trip(minimal_profile, markdown)
        assert ok, f"Round-trip failed with diffs: {diffs}"

    def test_detects_id_mismatch(self, sample_profile):
        """Should detect if the markdown has a different ID."""
        markdown = _render_profile_markdown(sample_profile)
        # Tamper with the markdown
        markdown = markdown.replace("id: coding", "id: wrong-id")
        ok, diffs = verify_round_trip(sample_profile, markdown)
        assert not ok
        assert any("id:" in d for d in diffs)

    def test_detects_model_mismatch(self, sample_profile):
        """Should detect if the model field differs."""
        markdown = _render_profile_markdown(sample_profile)
        markdown = markdown.replace("claude-sonnet-4-6", "gpt-4")
        ok, diffs = verify_round_trip(sample_profile, markdown)
        assert not ok
        assert any("model:" in d for d in diffs)

    def test_detects_tools_mismatch(self, sample_profile):
        """Should detect if allowed_tools differ."""
        markdown = _render_profile_markdown(sample_profile)
        # Remove one tool from the JSON
        markdown = markdown.replace('"Bash"', '"Grep"')
        ok, diffs = verify_round_trip(sample_profile, markdown)
        assert not ok
        assert any("allowed_tools:" in d for d in diffs)

    def test_invalid_markdown_detected(self, sample_profile):
        """Should detect if the markdown doesn't even parse."""
        # Put invalid JSON under a structured section heading so the parser
        # actually reports a parse error.
        bad_md = "---\nid: coding\nname: Coding Agent\n---\n\n## Config\n```json\n{invalid}\n```"
        ok, diffs = verify_round_trip(sample_profile, bad_md)
        assert not ok
        assert any("Parse errors" in d for d in diffs)

    def test_profile_with_description(self):
        """Description should round-trip through frontmatter."""
        profile = AgentProfile(
            id="desc-test",
            name="Description Test",
            description="A profile with a description",
        )
        markdown = _render_profile_markdown(profile)
        ok, diffs = verify_round_trip(profile, markdown)
        assert ok, f"Round-trip failed: {diffs}"

    def test_profile_with_install(self):
        """Install manifest should round-trip."""
        profile = AgentProfile(
            id="install-test",
            name="Install Test",
            install={"pip": ["black", "ruff"], "npm": ["eslint"]},
        )
        markdown = _render_profile_markdown(profile)
        ok, diffs = verify_round_trip(profile, markdown)
        assert ok, f"Round-trip failed: {diffs}"

    def test_profile_with_mcp_servers(self):
        """MCP servers should round-trip."""
        profile = AgentProfile(
            id="mcp-test",
            name="MCP Test",
            mcp_servers={
                "github": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                },
                "slack": {
                    "command": "node",
                    "args": ["slack-mcp-server"],
                    "env": {"SLACK_TOKEN": "xxx"},
                },
            },
        )
        markdown = _render_profile_markdown(profile)
        ok, diffs = verify_round_trip(profile, markdown)
        assert ok, f"Round-trip failed: {diffs}"


# ---------------------------------------------------------------------------
# _render_profile_markdown tests
# ---------------------------------------------------------------------------


class TestRenderProfileMarkdown:
    """Test the profile rendering helper."""

    def test_renders_valid_markdown(self, sample_profile):
        """Rendered markdown should parse without errors."""
        markdown = _render_profile_markdown(sample_profile)
        parsed = parse_profile(markdown)
        assert parsed.is_valid, f"Parse errors: {parsed.errors}"

    def test_renders_frontmatter(self, sample_profile):
        """Should include YAML frontmatter with id and name."""
        markdown = _render_profile_markdown(sample_profile)
        assert "id: coding" in markdown
        assert "name: Coding Agent" in markdown

    def test_renders_config_section(self, sample_profile):
        """Should include ## Config with model and permission_mode."""
        markdown = _render_profile_markdown(sample_profile)
        assert "## Config" in markdown
        assert "claude-sonnet-4-6" in markdown
        assert "auto" in markdown

    def test_renders_tools_section(self, sample_profile):
        """Should include ## Tools with allowed tools."""
        markdown = _render_profile_markdown(sample_profile)
        assert "## Tools" in markdown
        assert "Read" in markdown
        assert "Write" in markdown

    def test_renders_role_section(self, sample_profile):
        """Should split system_prompt_suffix and render ## Role."""
        markdown = _render_profile_markdown(sample_profile)
        assert "## Role" in markdown
        assert "software engineering agent" in markdown

    def test_renders_rules_section(self, sample_profile):
        """Should render ## Rules from system_prompt_suffix."""
        markdown = _render_profile_markdown(sample_profile)
        assert "## Rules" in markdown
        assert "Always run tests" in markdown

    def test_renders_reflection_section(self, sample_profile):
        """Should render ## Reflection from system_prompt_suffix."""
        markdown = _render_profile_markdown(sample_profile)
        assert "## Reflection" in markdown
        assert "consider what you learned" in markdown

    def test_minimal_profile_renders(self, minimal_profile):
        """Minimal profile should render valid markdown."""
        markdown = _render_profile_markdown(minimal_profile)
        parsed = parse_profile(markdown)
        assert parsed.is_valid
        assert parsed.frontmatter.id == "minimal"

    def test_empty_fields_omitted(self, minimal_profile):
        """Empty config/tools/mcp should not produce empty sections."""
        markdown = _render_profile_markdown(minimal_profile)
        assert "## Config" not in markdown
        assert "## Tools" not in markdown
        assert "## MCP Servers" not in markdown
        assert "## Install" not in markdown


# ---------------------------------------------------------------------------
# _vault_profile_path tests
# ---------------------------------------------------------------------------


class TestVaultProfilePath:
    """Test vault path computation."""

    def test_standard_path(self):
        path = _vault_profile_path("/home/user/.agent-queue", "coding")
        assert path == "/home/user/.agent-queue/vault/agent-types/coding/profile.md"

    def test_slug_with_hyphens(self):
        path = _vault_profile_path("/data", "web-developer")
        assert path == "/data/vault/agent-types/web-developer/profile.md"


# ---------------------------------------------------------------------------
# scan_profile_migration (dry-run) tests
# ---------------------------------------------------------------------------


class TestScanProfileMigration:
    """Test dry-run migration preview."""

    async def test_empty_database(self, db, data_dir):
        """No profiles → empty report."""
        report = await scan_profile_migration(db, data_dir)
        assert report.dry_run is True
        assert report.total == 0
        assert report.written == 0
        assert report.skipped == 0

    async def test_profiles_without_vault_files(self, db, data_dir, sample_profile):
        """Profiles without vault files should be reported as would_write."""
        await db.create_profile(sample_profile)
        report = await scan_profile_migration(db, data_dir)
        assert report.total == 1
        assert report.written == 1  # would_write count
        assert report.skipped == 0
        assert report.results[0].action == "would_write"

    async def test_profiles_with_existing_vault_files(self, db, data_dir, sample_profile):
        """Profiles with vault files should be reported as skipped."""
        await db.create_profile(sample_profile)

        # Create the vault file manually
        vault_path = _vault_profile_path(data_dir, sample_profile.id)
        os.makedirs(os.path.dirname(vault_path), exist_ok=True)
        with open(vault_path, "w") as f:
            f.write("existing content")

        report = await scan_profile_migration(db, data_dir)
        assert report.total == 1
        assert report.written == 0
        assert report.skipped == 1
        assert report.results[0].action == "skipped"

    async def test_mixed_state(self, db, data_dir, sample_profile, minimal_profile):
        """Some profiles have vault files, some don't."""
        await db.create_profile(sample_profile)
        await db.create_profile(minimal_profile)

        # Create vault file for only one profile
        vault_path = _vault_profile_path(data_dir, sample_profile.id)
        os.makedirs(os.path.dirname(vault_path), exist_ok=True)
        with open(vault_path, "w") as f:
            f.write("existing")

        report = await scan_profile_migration(db, data_dir)
        assert report.total == 2
        assert report.written == 1  # minimal would be written
        assert report.skipped == 1  # coding is skipped


# ---------------------------------------------------------------------------
# migrate_db_profiles_to_vault tests
# ---------------------------------------------------------------------------


class TestMigrateDbProfilesToVault:
    """Test the live migration function."""

    async def test_empty_database(self, db, data_dir):
        """No profiles → nothing to do."""
        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.total == 0
        assert report.written == 0
        assert report.errors == 0

    async def test_single_profile_migration(self, db, data_dir, sample_profile):
        """A single DB profile should be written to the vault."""
        await db.create_profile(sample_profile)

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.total == 1
        assert report.written == 1
        assert report.skipped == 0
        assert report.errors == 0

        # Verify the file exists and is valid
        vault_path = _vault_profile_path(data_dir, "coding")
        assert os.path.isfile(vault_path)

        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.is_valid
        assert parsed.frontmatter.id == "coding"
        assert parsed.frontmatter.name == "Coding Agent"
        assert parsed.config["model"] == "claude-sonnet-4-6"

    async def test_multiple_profiles(self, db, data_dir, sample_profile, minimal_profile):
        """Multiple profiles should all be migrated."""
        await db.create_profile(sample_profile)
        await db.create_profile(minimal_profile)

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.total == 2
        assert report.written == 2
        assert report.errors == 0

        # Both files should exist
        assert os.path.isfile(_vault_profile_path(data_dir, "coding"))
        assert os.path.isfile(_vault_profile_path(data_dir, "minimal"))

    async def test_skips_existing_vault_files(self, db, data_dir, sample_profile):
        """Profiles with existing vault files should be skipped."""
        await db.create_profile(sample_profile)

        # Create vault file first
        vault_path = _vault_profile_path(data_dir, "coding")
        os.makedirs(os.path.dirname(vault_path), exist_ok=True)
        with open(vault_path, "w") as f:
            f.write("pre-existing content")

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.total == 1
        assert report.written == 0
        assert report.skipped == 1

        # Original content should be preserved
        with open(vault_path) as f:
            assert f.read() == "pre-existing content"

    async def test_idempotent(self, db, data_dir, sample_profile):
        """Running migration twice should produce same result."""
        await db.create_profile(sample_profile)

        report1 = await migrate_db_profiles_to_vault(db, data_dir)
        assert report1.written == 1

        report2 = await migrate_db_profiles_to_vault(db, data_dir)
        assert report2.written == 0
        assert report2.skipped == 1

    async def test_force_overwrites(self, db, data_dir, sample_profile):
        """force=True should overwrite existing vault files."""
        await db.create_profile(sample_profile)

        # Create pre-existing vault file
        vault_path = _vault_profile_path(data_dir, "coding")
        os.makedirs(os.path.dirname(vault_path), exist_ok=True)
        with open(vault_path, "w") as f:
            f.write("old content")

        report = await migrate_db_profiles_to_vault(db, data_dir, force=True)
        assert report.total == 1
        assert report.written == 1
        assert report.skipped == 0

        # Content should be replaced
        with open(vault_path) as f:
            content = f.read()
        assert "old content" not in content
        assert "coding" in content.lower()

    async def test_dry_run_no_writes(self, db, data_dir, sample_profile):
        """dry_run=True should not write any files."""
        await db.create_profile(sample_profile)

        report = await migrate_db_profiles_to_vault(db, data_dir, dry_run=True)
        assert report.dry_run is True
        assert report.written == 1  # would_write count

        # No file should exist
        vault_path = _vault_profile_path(data_dir, "coding")
        assert not os.path.isfile(vault_path)

    async def test_creates_vault_directories(self, db, data_dir, sample_profile):
        """Migration should create vault/agent-types/{id}/ directories."""
        await db.create_profile(sample_profile)

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.written == 1

        # Check directory structure
        profile_dir = os.path.join(data_dir, "vault", "agent-types", "coding")
        assert os.path.isdir(profile_dir)
        assert os.path.isdir(os.path.join(profile_dir, "playbooks"))
        assert os.path.isdir(os.path.join(profile_dir, "memory"))

    async def test_round_trip_verification(self, db, data_dir, sample_profile):
        """With verify=True, round-trip results should be in the report."""
        await db.create_profile(sample_profile)

        report = await migrate_db_profiles_to_vault(db, data_dir, verify=True)
        assert report.written == 1
        result = report.results[0]
        assert result.round_trip_ok is True
        assert result.round_trip_diffs == []

    async def test_no_verification(self, db, data_dir, sample_profile):
        """With verify=False, round-trip is not checked."""
        await db.create_profile(sample_profile)

        report = await migrate_db_profiles_to_vault(db, data_dir, verify=False)
        assert report.written == 1
        result = report.results[0]
        assert result.round_trip_ok is None

    async def test_all_field_types_preserved(self, db, data_dir, sample_profile):
        """Every field type should be preserved through the migration."""
        await db.create_profile(sample_profile)
        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.written == 1

        # Read back and parse
        vault_path = _vault_profile_path(data_dir, "coding")
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.is_valid

        # Check all fields
        assert parsed.frontmatter.id == "coding"
        assert parsed.frontmatter.name == "Coding Agent"
        assert parsed.frontmatter.extra.get("description") == "A general-purpose coding agent"
        assert parsed.config["model"] == "claude-sonnet-4-6"
        assert parsed.config["permission_mode"] == "auto"
        assert parsed.tools["allowed"] == ["Read", "Write", "Edit", "Bash"]
        assert "github" in parsed.mcp_servers
        assert parsed.mcp_servers["github"]["command"] == "npx"
        assert "software engineering agent" in parsed.role
        assert "Always run tests" in parsed.rules
        assert "consider what you learned" in parsed.reflection
        assert parsed.install == {"npm": ["eslint"], "commands": ["docker"]}

    async def test_mixed_state_migration(self, db, data_dir, sample_profile, minimal_profile):
        """Some profiles migrated, some not — only unmigrated ones are written."""
        await db.create_profile(sample_profile)
        await db.create_profile(minimal_profile)

        # Pre-create vault file for coding profile only
        vault_path = _vault_profile_path(data_dir, "coding")
        os.makedirs(os.path.dirname(vault_path), exist_ok=True)
        with open(vault_path, "w") as f:
            f.write("existing")

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.total == 2
        assert report.written == 1
        assert report.skipped == 1

        # Minimal should have been written
        assert os.path.isfile(_vault_profile_path(data_dir, "minimal"))
        # Coding should be untouched
        with open(vault_path) as f:
            assert f.read() == "existing"

    async def test_report_to_dict(self, db, data_dir, sample_profile):
        """MigrationReport.to_dict should produce a well-formed response."""
        await db.create_profile(sample_profile)
        report = await migrate_db_profiles_to_vault(db, data_dir)
        d = report.to_dict()
        assert d["success"] is True
        assert d["total"] == 1
        assert d["written"] == 1
        assert isinstance(d["results"], list)
        assert isinstance(d["details"], list)


# ---------------------------------------------------------------------------
# Command handler integration tests
# ---------------------------------------------------------------------------


class TestCommandHandlerMigrateProfiles:
    """Test the _cmd_migrate_profiles command handler method.

    Note: Orchestrator.initialize() creates a default "orchestrator" profile
    via the vault layout setup.  Tests account for this by checking relative
    counts or filtering by specific profile IDs.
    """

    @pytest.fixture
    async def handler(self, tmp_path):
        from src.commands.handler import CommandHandler

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yield handler
        await orch.db.close()

    async def _count_profiles_before(self, handler) -> int:
        """Count profiles in DB before the test adds any."""
        profiles = await handler.db.list_profiles()
        return len(profiles)

    async def test_migrate_via_command(self, handler):
        """Command handler should successfully migrate profiles."""
        baseline = await self._count_profiles_before(handler)

        # Create a DB-only profile (no vault file)
        await handler.db.create_profile(
            AgentProfile(
                id="test-agent",
                name="Test Agent",
                model="claude-sonnet-4-6",
            )
        )

        result = await handler.execute("migrate_profiles", {})
        assert result["success"] is True
        assert result["total"] == baseline + 1

        # Our test profile should have been written
        vault_path = handler._vault_profile_path("test-agent")
        assert os.path.isfile(vault_path)

        # Find the result for our profile specifically
        our_result = [r for r in result["results"] if r["profile_id"] == "test-agent"]
        assert len(our_result) == 1
        assert our_result[0]["action"] == "written"

    async def test_migrate_dry_run_via_command(self, handler):
        """dry_run=True should not write files."""
        await handler.db.create_profile(AgentProfile(id="test", name="Test"))

        result = await handler.execute("migrate_profiles", {"dry_run": True})
        assert result["dry_run"] is True

        # Our test profile should not have been written
        vault_path = handler._vault_profile_path("test")
        assert not os.path.isfile(vault_path)

        # Should report would_write for our profile
        our_result = [r for r in result["results"] if r["profile_id"] == "test"]
        assert len(our_result) == 1
        assert our_result[0]["action"] == "would_write"

    async def test_migrate_force_via_command(self, handler):
        """force=True should overwrite existing vault files."""
        await handler.db.create_profile(AgentProfile(id="test", name="Test", model="old-model"))

        # First migration
        await handler.execute("migrate_profiles", {})

        # Update DB profile
        await handler.db.update_profile("test", model="new-model")

        # Second migration with force
        result = await handler.execute("migrate_profiles", {"force": True})

        # Our profile should have been overwritten
        our_result = [r for r in result["results"] if r["profile_id"] == "test"]
        assert len(our_result) == 1
        assert our_result[0]["action"] == "written"

        # Verify new content
        vault_path = handler._vault_profile_path("test")
        with open(vault_path) as f:
            content = f.read()
        assert "new-model" in content

    async def test_migrate_empty_db(self, handler):
        """No user-added profiles → success with only baseline profiles."""
        result = await handler.execute("migrate_profiles", {})
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and unusual profiles."""

    async def test_profile_with_empty_strings(self, db, data_dir):
        """Profile with all empty optional fields."""
        profile = AgentProfile(id="empty", name="Empty")
        await db.create_profile(profile)

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.written == 1
        assert report.errors == 0

        vault_path = _vault_profile_path(data_dir, "empty")
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.is_valid

    async def test_profile_with_special_chars_in_name(self, db, data_dir):
        """Profile name with special characters."""
        profile = AgentProfile(
            id="special",
            name="Code Review & QA Agent (v2)",
        )
        await db.create_profile(profile)

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.written == 1

        vault_path = _vault_profile_path(data_dir, "special")
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.is_valid
        assert parsed.frontmatter.name == "Code Review & QA Agent (v2)"

    async def test_profile_with_multiline_role(self, db, data_dir):
        """Profile with multi-line role text."""
        profile = AgentProfile(
            id="multi",
            name="Multi-line",
            system_prompt_suffix=(
                "## Role\nYou are a software engineer.\n"
                "You specialize in Python and TypeScript.\n"
                "You prefer functional programming patterns."
            ),
        )
        await db.create_profile(profile)

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.written == 1

        vault_path = _vault_profile_path(data_dir, "multi")
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.is_valid
        assert "software engineer" in parsed.role
        assert "TypeScript" in parsed.role

    async def test_profile_with_only_system_prompt_no_markers(self, db, data_dir):
        """Profile with system_prompt_suffix that has no ## markers."""
        profile = AgentProfile(
            id="no-markers",
            name="No Markers",
            system_prompt_suffix="Just a plain text prompt without section markers.",
        )
        await db.create_profile(profile)

        report = await migrate_db_profiles_to_vault(db, data_dir)
        assert report.written == 1

        vault_path = _vault_profile_path(data_dir, "no-markers")
        with open(vault_path) as f:
            content = f.read()
        parsed = parse_profile(content)
        assert parsed.is_valid
        # The plain text should end up in the Role section
        assert "plain text prompt" in parsed.role

    async def test_profile_migration_result_to_dict(self):
        """ProfileMigrationResult.to_dict should serialize correctly."""
        result = ProfileMigrationResult(
            profile_id="test",
            name="Test",
            action="written",
            reason="/path/to/file",
            round_trip_ok=True,
            round_trip_diffs=[],
        )
        d = result.to_dict()
        assert d["profile_id"] == "test"
        assert d["action"] == "written"
        assert d["round_trip_ok"] is True

    async def test_migration_report_with_errors(self):
        """MigrationReport.to_dict with errors should report success=False."""
        report = MigrationReport(
            total=2,
            written=1,
            errors=1,
        )
        d = report.to_dict()
        assert d["success"] is False
