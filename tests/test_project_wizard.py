"""Tests for the Discord project wizard UI components.

Covers:
- ProjectWizardState dataclass and slug generation
- State management (get_or_create, cleanup)
- Summary embed building
- ProjectInfoModal on_submit flow
- RepoChoiceView button callbacks
- GitHubRepoModal on_submit
- ExistingRepoModal on_submit
- WorkspaceCountView callbacks
- WizardExecutor success path
- WizardExecutor rollback on failure
- WizardExecutor partial rollback (workspace failure mid-way)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from src.discord.project_wizard import (
    ProjectWizardState,
    ProjectInfoModal,
    RepoChoiceView,
    GitHubRepoModal,
    ExistingRepoModal,
    WorkspaceCountView,
    WorkspaceLocationView,
    WorkspaceLocationModal,
    WizardError,
    _cleanup_state,
    _execute_wizard,
    _get_or_create_state,
    _state_summary_embed,
    _wizard_states,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_wizard_states():
    """Ensure wizard state dict is clean before/after each test."""
    _wizard_states.clear()
    yield
    _wizard_states.clear()


@pytest.fixture
def mock_handler():
    handler = AsyncMock()
    handler.execute = AsyncMock(return_value={})
    return handler


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.config.discord.per_project_channels.auto_create = True
    bot.config.discord.per_project_channels.naming_convention = "{project_id}"
    bot.config.discord.per_project_channels.category_name = ""
    return bot


@pytest.fixture
def mock_interaction():
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 12345
    interaction.guild = MagicMock(spec=discord.Guild)
    # Set up guild.create_text_channel to return a mock channel
    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.id = 999888777
    mock_channel.mention = "#mock-channel"
    interaction.guild.create_text_channel = AsyncMock(return_value=mock_channel)
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


# ---------------------------------------------------------------------------
# ProjectWizardState
# ---------------------------------------------------------------------------


class TestProjectWizardState:
    def test_default_values(self):
        state = ProjectWizardState(user_id=1)
        assert state.project_name == ""
        assert state.default_branch == "main"
        assert state.workspace_count == 3
        assert state.create_repo is False

    def test_project_id_slug_simple(self):
        state = ProjectWizardState(user_id=1, project_name="My Awesome App")
        assert state.project_id == "my-awesome-app"

    def test_project_id_slug_special_chars(self):
        state = ProjectWizardState(user_id=1, project_name="Test @ Project #1!")
        assert state.project_id == "test-project-1"

    def test_project_id_empty_name(self):
        state = ProjectWizardState(user_id=1, project_name="")
        assert state.project_id == "project"

    def test_project_id_strips_leading_trailing_hyphens(self):
        state = ProjectWizardState(user_id=1, project_name="  --test--  ")
        assert state.project_id == "test"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


class TestStateManagement:
    def test_get_or_create_new(self):
        state = _get_or_create_state(999)
        assert state.user_id == 999
        assert 999 in _wizard_states

    def test_get_or_create_existing(self):
        _wizard_states[999] = ProjectWizardState(user_id=999, project_name="existing")
        state = _get_or_create_state(999)
        assert state.project_name == "existing"

    def test_cleanup_state(self):
        _wizard_states[999] = ProjectWizardState(user_id=999)
        _cleanup_state(999)
        assert 999 not in _wizard_states

    def test_cleanup_nonexistent(self):
        # Should not raise
        _cleanup_state(99999)


# ---------------------------------------------------------------------------
# Summary embed
# ---------------------------------------------------------------------------


class TestStateSummaryEmbed:
    def test_minimal_embed(self):
        state = ProjectWizardState(user_id=1, project_name="Test")
        embed = _state_summary_embed(state)
        assert embed.title == "New Project Wizard"
        assert any(f.name == "Project" for f in embed.fields)

    def test_full_state_embed(self):
        state = ProjectWizardState(
            user_id=1,
            project_name="Full Project",
            description="A description",
            tech_stack="Python, React",
            default_branch="develop",
            repo_url="https://github.com/user/repo",
        )
        embed = _state_summary_embed(state)
        field_names = [f.name for f in embed.fields]
        assert "Description" in field_names
        assert "Tech Stack" in field_names
        assert "Branch" in field_names
        assert "Repository" in field_names

    def test_create_repo_embed(self):
        state = ProjectWizardState(
            user_id=1,
            project_name="Test",
            create_repo=True,
            repo_name="test-repo",
            repo_org="myorg",
            repo_visibility="public",
        )
        embed = _state_summary_embed(state)
        repo_field = next(f for f in embed.fields if f.name == "Repository")
        assert "myorg/test-repo" in repo_field.value
        assert "public" in repo_field.value

    def test_workspace_location_shown(self):
        state = ProjectWizardState(
            user_id=1,
            project_name="Test",
            workspace_root="/custom/path",
        )
        embed = _state_summary_embed(state)
        field_names = [f.name for f in embed.fields]
        assert "Workspace Location" in field_names
        loc_field = next(f for f in embed.fields if f.name == "Workspace Location")
        assert loc_field.value == "/custom/path"

    def test_default_workspace_location_not_shown(self):
        state = ProjectWizardState(
            user_id=1,
            project_name="Test",
        )
        embed = _state_summary_embed(state)
        field_names = [f.name for f in embed.fields]
        assert "Workspace Location" not in field_names

    def test_long_description_truncated(self):
        state = ProjectWizardState(
            user_id=1,
            project_name="Test",
            description="x" * 300,
        )
        embed = _state_summary_embed(state)
        desc_field = next(f for f in embed.fields if f.name == "Description")
        assert len(desc_field.value) <= 204  # 200 + "..."


# ---------------------------------------------------------------------------
# ProjectInfoModal
# ---------------------------------------------------------------------------


class TestProjectInfoModal:
    @pytest.mark.asyncio
    async def test_on_submit_stores_state(self, mock_handler, mock_bot, mock_interaction):
        modal = ProjectInfoModal(mock_handler, mock_bot)
        modal.name_input._value = "My Project"
        modal.description_input._value = "A description"
        modal.tech_stack_input._value = "Python"
        modal.default_branch_input._value = "develop"

        await modal.on_submit(mock_interaction)

        state = _wizard_states.get(12345)
        assert state is not None
        assert state.project_name == "My Project"
        assert state.description == "A description"
        assert state.tech_stack == "Python"
        assert state.default_branch == "develop"

    @pytest.mark.asyncio
    async def test_on_submit_empty_name(self, mock_handler, mock_bot, mock_interaction):
        modal = ProjectInfoModal(mock_handler, mock_bot)
        modal.name_input._value = "   "
        modal.description_input._value = ""
        modal.tech_stack_input._value = ""
        modal.default_branch_input._value = ""

        await modal.on_submit(mock_interaction)

        # Should respond with error, not create repo choice view
        mock_interaction.response.send_message.assert_called_once()
        call_kwargs = mock_interaction.response.send_message.call_args
        assert "required" in call_kwargs[1].get("content", call_kwargs[0][0]).lower()

    @pytest.mark.asyncio
    async def test_on_submit_default_branch(self, mock_handler, mock_bot, mock_interaction):
        modal = ProjectInfoModal(mock_handler, mock_bot)
        modal.name_input._value = "Test"
        modal.description_input._value = ""
        modal.tech_stack_input._value = ""
        modal.default_branch_input._value = ""

        await modal.on_submit(mock_interaction)

        state = _wizard_states[12345]
        assert state.default_branch == "main"


# ---------------------------------------------------------------------------
# RepoChoiceView
# ---------------------------------------------------------------------------


class TestRepoChoiceView:
    @pytest.mark.asyncio
    async def test_skip_repo_sets_state(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="Test",
        )
        view = RepoChoiceView(mock_handler, mock_bot)
        await view.skip_repo_btn.callback(mock_interaction)

        state = _wizard_states[12345]
        assert state.repo_url == ""
        assert state.create_repo is False
        mock_interaction.response.edit_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_cleans_state(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(user_id=12345)
        view = RepoChoiceView(mock_handler, mock_bot)
        await view.cancel_btn.callback(mock_interaction)

        assert 12345 not in _wizard_states
        mock_interaction.response.edit_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_repo_opens_modal(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="Test",
        )
        view = RepoChoiceView(mock_handler, mock_bot)
        await view.create_repo_btn.callback(mock_interaction)
        mock_interaction.response.send_modal.assert_called_once()

    @pytest.mark.asyncio
    async def test_existing_repo_opens_modal(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="Test",
        )
        view = RepoChoiceView(mock_handler, mock_bot)
        await view.existing_repo_btn.callback(mock_interaction)
        mock_interaction.response.send_modal.assert_called_once()


# ---------------------------------------------------------------------------
# GitHubRepoModal
# ---------------------------------------------------------------------------


class TestGitHubRepoModal:
    @pytest.mark.asyncio
    async def test_on_submit_stores_repo_info(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="Test",
        )
        modal = GitHubRepoModal(mock_handler, mock_bot, default_name="test")
        modal.repo_name_input._value = "custom-repo"
        modal.visibility_input._value = "public"
        modal.org_input._value = "my-org"

        await modal.on_submit(mock_interaction)

        state = _wizard_states[12345]
        assert state.create_repo is True
        assert state.repo_name == "custom-repo"
        assert state.repo_visibility == "public"
        assert state.repo_org == "my-org"

    @pytest.mark.asyncio
    async def test_on_submit_defaults(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="My App",
        )
        modal = GitHubRepoModal(mock_handler, mock_bot)
        modal.repo_name_input._value = ""
        modal.visibility_input._value = ""
        modal.org_input._value = ""

        await modal.on_submit(mock_interaction)

        state = _wizard_states[12345]
        assert state.repo_name == "my-app"  # Falls back to project_id
        assert state.repo_visibility == "private"
        assert state.repo_org == ""

    @pytest.mark.asyncio
    async def test_on_submit_invalid_visibility(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="Test",
        )
        modal = GitHubRepoModal(mock_handler, mock_bot)
        modal.repo_name_input._value = "repo"
        modal.visibility_input._value = "invalid"
        modal.org_input._value = ""

        await modal.on_submit(mock_interaction)

        state = _wizard_states[12345]
        assert state.repo_visibility == "private"  # Defaults to private


# ---------------------------------------------------------------------------
# ExistingRepoModal
# ---------------------------------------------------------------------------


class TestExistingRepoModal:
    @pytest.mark.asyncio
    async def test_on_submit_stores_url(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="Test",
        )
        modal = ExistingRepoModal(mock_handler, mock_bot)
        modal.repo_url_input._value = "https://github.com/user/repo.git"

        await modal.on_submit(mock_interaction)

        state = _wizard_states[12345]
        assert state.repo_url == "https://github.com/user/repo.git"
        assert state.create_repo is False


# ---------------------------------------------------------------------------
# WorkspaceCountView
# ---------------------------------------------------------------------------


class TestWorkspaceCountView:
    @pytest.mark.asyncio
    async def test_has_four_count_buttons_plus_cancel(self, mock_handler, mock_bot):
        view = WorkspaceCountView(mock_handler, mock_bot)
        # 4 count buttons (2,3,4,5) + 1 cancel button = 5 total
        assert len(view.children) == 5

    @pytest.mark.asyncio
    async def test_cancel_cleans_state(self, mock_handler, mock_bot, mock_interaction):
        _wizard_states[12345] = ProjectWizardState(user_id=12345)
        view = WorkspaceCountView(mock_handler, mock_bot)
        await view.cancel_btn.callback(mock_interaction)
        assert 12345 not in _wizard_states


# ---------------------------------------------------------------------------
# WorkspaceLocationView
# ---------------------------------------------------------------------------


class TestWorkspaceLocationView:
    @pytest.mark.asyncio
    async def test_default_location_sets_empty_root(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        """Default location button leaves workspace_root empty and executes."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="Test",
            workspace_count=2,
        )
        _wizard_states[12345] = state

        mock_handler.execute = AsyncMock(
            side_effect=[
                {"created": "test"},
                {"created": "ws-001"},
                {"created": "ws-002"},
            ]
        )

        view = WorkspaceLocationView(mock_handler, mock_bot)
        await view.default_location_btn.callback(mock_interaction)

        assert state.workspace_root == ""
        # Should have proceeded to execute (deferred response)
        mock_interaction.response.defer.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_location_opens_modal(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="Test",
        )
        view = WorkspaceLocationView(mock_handler, mock_bot)
        await view.custom_location_btn.callback(mock_interaction)
        mock_interaction.response.send_modal.assert_called_once()
        modal = mock_interaction.response.send_modal.call_args[0][0]
        assert isinstance(modal, WorkspaceLocationModal)

    @pytest.mark.asyncio
    async def test_cancel_cleans_state(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        _wizard_states[12345] = ProjectWizardState(user_id=12345)
        view = WorkspaceLocationView(mock_handler, mock_bot)
        await view.cancel_btn.callback(mock_interaction)
        assert 12345 not in _wizard_states


# ---------------------------------------------------------------------------
# WorkspaceLocationModal
# ---------------------------------------------------------------------------


class TestWorkspaceLocationModal:
    @pytest.mark.asyncio
    async def test_on_submit_stores_custom_path(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        state = ProjectWizardState(
            user_id=12345,
            project_name="Test",
            workspace_count=2,
        )
        _wizard_states[12345] = state

        mock_handler.execute = AsyncMock(
            side_effect=[
                {"created": "test"},
                {"created": "ws-001"},
                {"created": "ws-002"},
                # Step 4: set_project_channel
                {"ok": True},
            ]
        )

        modal = WorkspaceLocationModal(mock_handler, mock_bot)
        modal.location_input._value = "/custom/workspace/path"

        await modal.on_submit(mock_interaction)

        assert state.workspace_root == "/custom/workspace/path"
        # Should have proceeded to execute
        mock_interaction.response.defer.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_submit_empty_path_rejected(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        _wizard_states[12345] = ProjectWizardState(
            user_id=12345,
            project_name="Test",
        )
        modal = WorkspaceLocationModal(mock_handler, mock_bot)
        modal.location_input._value = "   "

        await modal.on_submit(mock_interaction)

        # Should show error, not proceed to execute
        mock_interaction.response.send_message.assert_called_once()
        call_kwargs = mock_interaction.response.send_message.call_args
        assert "required" in call_kwargs[0][0].lower()


# ---------------------------------------------------------------------------
# WizardExecutor — success
# ---------------------------------------------------------------------------


class TestWizardExecutorSuccess:
    @pytest.mark.asyncio
    async def test_full_flow_with_repo(self, mock_handler, mock_bot, mock_interaction):
        """Full wizard: create repo, project, workspaces, README."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="My App",
            description="A test app",
            tech_stack="Python",
            create_repo=True,
            repo_name="my-app",
            repo_visibility="private",
            workspace_count=2,
        )

        mock_handler.execute = AsyncMock(
            side_effect=[
                # Step 1: create_github_repo
                {"repo_url": "https://github.com/user/my-app"},
                # Step 2: create_project
                {"created": "my-app", "auto_create_channels": True},
                # Step 3: add_workspace x2
                {"created": "ws-001"},
                {"created": "ws-002"},
                # Step 4: set_project_channel (auto-create channel)
                {"ok": True},
                # Step 5: generate_readme
                {"committed": True, "pushed": True},
            ]
        )

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        # Verify all commands were called
        calls = mock_handler.execute.call_args_list
        assert calls[0][0][0] == "create_github_repo"
        assert calls[1][0][0] == "create_project"
        assert calls[2][0][0] == "add_workspace"
        assert calls[3][0][0] == "add_workspace"
        assert calls[4][0][0] == "set_project_channel"
        assert calls[5][0][0] == "generate_readme"

        # State should be cleaned up
        assert 12345 not in _wizard_states

    @pytest.mark.asyncio
    async def test_flow_without_repo(self, mock_handler, mock_bot, mock_interaction):
        """Wizard without repo: no GitHub repo creation, no README."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="Local Project",
            workspace_count=3,
        )

        mock_handler.execute = AsyncMock(
            side_effect=[
                # Step 2: create_project
                {"created": "local-project"},
                # Step 3: add_workspace x3
                {"created": "ws-001"},
                {"created": "ws-002"},
                {"created": "ws-003"},
                # Step 4: set_project_channel (auto-create channel)
                {"ok": True},
            ]
        )

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        calls = mock_handler.execute.call_args_list
        assert calls[0][0][0] == "create_project"
        assert calls[4][0][0] == "set_project_channel"
        assert len(calls) == 5  # 1 project + 3 workspaces + 1 channel, no readme

    @pytest.mark.asyncio
    async def test_custom_workspace_root_passes_path(
        self, mock_handler, mock_bot, mock_interaction
    ):
        """Custom workspace_root should pass a 'path' arg to add_workspace."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="My App",
            workspace_count=2,
            workspace_root="/custom/workspaces",
        )

        mock_handler.execute = AsyncMock(
            side_effect=[
                {"created": "my-app"},
                {"created": "ws-001"},
                {"created": "ws-002"},
                # Step 4: set_project_channel
                {"ok": True},
            ]
        )

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        calls = mock_handler.execute.call_args_list
        # Workspace calls should include a 'path' arg
        ws_call_1 = calls[1][0][1]  # args dict for first add_workspace
        ws_call_2 = calls[2][0][1]  # args dict for second add_workspace
        assert "path" in ws_call_1
        assert "path" in ws_call_2
        assert ws_call_1["path"].startswith("/custom/workspaces/my-app/")
        assert ws_call_2["path"].startswith("/custom/workspaces/my-app/")
        # Paths should be different
        assert ws_call_1["path"] != ws_call_2["path"]

    @pytest.mark.asyncio
    async def test_default_workspace_root_no_path(self, mock_handler, mock_bot, mock_interaction):
        """Default workspace_root (empty) should NOT pass a 'path' arg."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="My App",
            workspace_count=1,
        )

        mock_handler.execute = AsyncMock(
            side_effect=[
                {"created": "my-app"},
                {"created": "ws-001"},
                # Step 4: set_project_channel
                {"ok": True},
            ]
        )

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        calls = mock_handler.execute.call_args_list
        ws_call = calls[1][0][1]  # args dict for add_workspace
        assert "path" not in ws_call

    @pytest.mark.asyncio
    async def test_readme_failure_is_non_fatal(self, mock_handler, mock_bot, mock_interaction):
        """README generation failure should not fail the wizard."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="Test",
            repo_url="https://github.com/user/test",
            workspace_count=1,
        )

        mock_handler.execute = AsyncMock(
            side_effect=[
                {"created": "test"},
                {"created": "ws-001"},
                # Step 4: set_project_channel
                {"ok": True},
                {"error": "README generation failed"},  # Non-fatal
            ]
        )

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        # Should NOT have triggered rollback (no remove_workspace calls)
        calls = mock_handler.execute.call_args_list
        assert len(calls) == 4
        assert all(c[0][0] != "remove_workspace" for c in calls)


# ---------------------------------------------------------------------------
# WizardExecutor — rollback
# ---------------------------------------------------------------------------


class TestWizardExecutorRollback:
    @pytest.mark.asyncio
    async def test_rollback_on_project_creation_failure(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        """If project creation fails, no rollback needed (nothing created yet)."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="Fail Project",
            workspace_count=2,
        )

        mock_handler.execute = AsyncMock(
            return_value={
                "error": "Duplicate project ID",
            }
        )

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        # Only one call (create_project), no rollback needed
        assert mock_handler.execute.call_count == 1
        assert 12345 not in _wizard_states

    @pytest.mark.asyncio
    async def test_rollback_on_workspace_failure(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        """If workspace creation fails mid-way, earlier workspaces and project are rolled back."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="Partial Fail",
            workspace_count=3,
        )

        mock_handler.execute = AsyncMock(
            side_effect=[
                # create_project
                {"created": "partial-fail"},
                # add_workspace #1 (success)
                {"created": "ws-001"},
                # add_workspace #2 (fail)
                {"error": "Clone failed: disk full"},
                # Rollback: remove_workspace ws-001
                {"deleted": "ws-001"},
                # Rollback: delete_project
                {"deleted": "partial-fail"},
            ]
        )

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        calls = mock_handler.execute.call_args_list
        # Verify rollback calls
        rollback_calls = [c for c in calls if c[0][0] in ("remove_workspace", "delete_project")]
        assert len(rollback_calls) == 2

        # Verify workspace removal
        ws_removal = next(c for c in calls if c[0][0] == "remove_workspace")
        assert ws_removal[0][1]["workspace_id"] == "ws-001"

        # Verify project deletion
        proj_deletion = next(c for c in calls if c[0][0] == "delete_project")
        assert proj_deletion[0][1]["project_id"] == "partial-fail"

    @pytest.mark.asyncio
    async def test_rollback_on_github_repo_failure(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        """If GitHub repo creation fails, nothing else was created."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="Test",
            create_repo=True,
            repo_name="test",
            workspace_count=2,
        )

        mock_handler.execute = AsyncMock(
            return_value={
                "error": "gh: not authenticated",
            }
        )

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        # Only the github repo creation attempt
        assert mock_handler.execute.call_count == 1
        assert mock_handler.execute.call_args[0][0] == "create_github_repo"

    @pytest.mark.asyncio
    async def test_state_cleaned_after_failure(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        """Wizard state is always cleaned up, even on failure."""
        state = ProjectWizardState(user_id=12345, project_name="Test")
        _wizard_states[12345] = state

        mock_handler.execute = AsyncMock(return_value={"error": "fail"})

        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)

        assert 12345 not in _wizard_states

    @pytest.mark.asyncio
    async def test_rollback_handles_removal_errors(
        self,
        mock_handler,
        mock_bot,
        mock_interaction,
    ):
        """Rollback should continue even if individual removal calls fail."""
        state = ProjectWizardState(
            user_id=12345,
            project_name="Test",
            workspace_count=2,
        )

        call_count = 0

        async def side_effect(name, args):
            nonlocal call_count
            call_count += 1
            if name == "create_project":
                return {"created": "test"}
            if name == "add_workspace":
                if call_count == 3:  # Second workspace fails
                    return {"error": "fail"}
                return {"created": "ws-001"}
            if name == "remove_workspace":
                raise Exception("removal also failed")
            if name == "delete_project":
                return {"deleted": "test"}
            return {}

        mock_handler.execute = AsyncMock(side_effect=side_effect)

        # Should not raise despite rollback errors
        await _execute_wizard(mock_interaction, state, mock_handler, mock_bot)
        assert 12345 not in _wizard_states
