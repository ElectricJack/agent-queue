"""New Project Wizard -- interactive Discord UI flow for project creation.

Guides users through creating a fully-configured project via a series of
modals and button views.  The wizard collects project info, repository
options, and workspace count, then delegates all business logic to the
shared ``CommandHandler`` via ``execute()``.

Flow:
    /new-project
     -> ProjectInfoModal (name, description, tech_stack, default_branch)
     -> RepoChoiceView (Create GitHub Repo | Use Existing | Skip)
     -> GitHubRepoModal / ExistingRepoModal (optional)
     -> WorkspaceCountView (2/3/4/5)
     -> WorkspaceLocationView (Default Location | Custom Location)
     -> WorkspaceLocationModal (optional, custom path)
     -> WizardExecutor (creates project, clones workspaces, channel, README)
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field

import discord

from src.discord.embeds import success_embed, error_embed, info_embed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wizard state
# ---------------------------------------------------------------------------

# In-memory store keyed by Discord user ID.  Cleaned up on completion,
# cancellation, or timeout.
_wizard_states: dict[int, "ProjectWizardState"] = {}

# Wizard timeout: 15 minutes (Discord modal limit)
_WIZARD_TIMEOUT_SECONDS = 900


@dataclass
class ProjectWizardState:
    """Holds all wizard-collected data for a single user's session."""

    user_id: int
    project_name: str = ""
    description: str = ""
    tech_stack: str = ""
    default_branch: str = "main"
    # Repo options
    repo_url: str = ""
    create_repo: bool = False
    repo_name: str = ""
    repo_visibility: str = "private"
    repo_org: str = ""
    # Workspace
    workspace_count: int = 3
    workspace_root: str = ""  # Custom workspace root; empty = use default
    # Timestamp for timeout tracking
    started_at: float = field(default_factory=time.time)

    @property
    def project_id(self) -> str:
        """Derive a slug ID from the project name."""
        slug = self.project_name.lower().strip()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = slug.strip("-")
        return slug or "project"


def _get_or_create_state(user_id: int) -> ProjectWizardState:
    """Get or create wizard state for a user."""
    if user_id not in _wizard_states:
        _wizard_states[user_id] = ProjectWizardState(user_id=user_id)
    return _wizard_states[user_id]


def _cleanup_state(user_id: int) -> None:
    """Remove wizard state for a user."""
    _wizard_states.pop(user_id, None)


def _state_summary_embed(state: ProjectWizardState) -> discord.Embed:
    """Build a summary embed showing the current wizard state."""
    embed = discord.Embed(
        title="New Project Wizard",
        color=0x3498DB,
    )
    if state.project_name:
        embed.add_field(name="Project", value=state.project_name, inline=True)
    if state.project_id and state.project_name:
        embed.add_field(name="ID", value=f"`{state.project_id}`", inline=True)
    if state.description:
        desc = state.description[:200] + ("..." if len(state.description) > 200 else "")
        embed.add_field(name="Description", value=desc, inline=False)
    if state.tech_stack:
        embed.add_field(name="Tech Stack", value=state.tech_stack, inline=True)
    if state.default_branch != "main":
        embed.add_field(name="Branch", value=state.default_branch, inline=True)
    if state.workspace_root:
        embed.add_field(
            name="Workspace Location",
            value=state.workspace_root,
            inline=False,
        )
    if state.repo_url:
        embed.add_field(name="Repository", value=state.repo_url, inline=False)
    elif state.create_repo:
        name = state.repo_name or state.project_id
        org_prefix = f"{state.repo_org}/" if state.repo_org else ""
        vis = state.repo_visibility
        embed.add_field(
            name="Repository",
            value=f"Will create: `{org_prefix}{name}` ({vis})",
            inline=False,
        )
    return embed


# ---------------------------------------------------------------------------
# Modal 1: Project Information
# ---------------------------------------------------------------------------


class ProjectInfoModal(discord.ui.Modal, title="Project Information"):
    """First step: collect project name, description, tech stack, branch."""

    name_input = discord.ui.TextInput(
        label="Project Name",
        placeholder="My Awesome App",
        required=True,
        max_length=100,
    )

    description_input = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.long,
        placeholder="A brief description of this project...",
        required=False,
        max_length=1000,
    )

    tech_stack_input = discord.ui.TextInput(
        label="Tech Stack",
        placeholder="TypeScript, React, Node.js",
        required=False,
        max_length=200,
    )

    default_branch_input = discord.ui.TextInput(
        label="Default Branch",
        placeholder="main",
        default="main",
        required=False,
        max_length=50,
    )

    def __init__(self, handler, bot) -> None:
        super().__init__()
        self._handler = handler
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state = _get_or_create_state(interaction.user.id)
        state.project_name = self.name_input.value.strip()
        state.description = self.description_input.value.strip()
        state.tech_stack = self.tech_stack_input.value.strip()
        state.default_branch = self.default_branch_input.value.strip() or "main"

        if not state.project_name:
            await interaction.response.send_message(
                "Project name is required.",
                ephemeral=True,
            )
            return

        embed = _state_summary_embed(state)
        embed.add_field(
            name="Repository Options",
            value="Choose how to set up the project repository:",
            inline=False,
        )
        view = RepoChoiceView(self._handler, self._bot)
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# View: Repository choice (Create / Use Existing / Skip)
# ---------------------------------------------------------------------------


class RepoChoiceView(discord.ui.View):
    """Three buttons for repository setup choice."""

    def __init__(self, handler, bot) -> None:
        super().__init__(timeout=_WIZARD_TIMEOUT_SECONDS)
        self._handler = handler
        self._bot = bot

    @discord.ui.button(
        label="Create GitHub Repo",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f4e6",
        row=0,
    )
    async def create_repo_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        state = _get_or_create_state(interaction.user.id)
        modal = GitHubRepoModal(self._handler, self._bot, default_name=state.project_id)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Use Existing Repo",
        style=discord.ButtonStyle.secondary,
        emoji="\U0001f517",
        row=0,
    )
    async def existing_repo_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        modal = ExistingRepoModal(self._handler, self._bot)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Skip (No Repo)",
        style=discord.ButtonStyle.secondary,
        emoji="\u23ed\ufe0f",
        row=0,
    )
    async def skip_repo_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        state = _get_or_create_state(interaction.user.id)
        state.repo_url = ""
        state.create_repo = False

        embed = _state_summary_embed(state)
        embed.add_field(
            name="Workspaces",
            value="How many workspaces for parallel agent execution?",
            inline=False,
        )
        view = WorkspaceCountView(self._handler, self._bot)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def cancel_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        _cleanup_state(interaction.user.id)
        await interaction.response.edit_message(
            content="Project wizard cancelled.",
            embed=None,
            view=None,
        )

    async def on_timeout(self) -> None:
        # State cleanup happens naturally; views just become inactive
        pass


# ---------------------------------------------------------------------------
# Modal 2a: GitHub Repository Creation
# ---------------------------------------------------------------------------


class GitHubRepoModal(discord.ui.Modal, title="GitHub Repository"):
    """Collect GitHub repo creation details."""

    repo_name_input = discord.ui.TextInput(
        label="Repository Name",
        placeholder="my-awesome-app",
        required=False,
        max_length=100,
    )

    visibility_input = discord.ui.TextInput(
        label="Visibility (private or public)",
        placeholder="private",
        default="private",
        required=False,
        max_length=10,
    )

    org_input = discord.ui.TextInput(
        label="GitHub Organization",
        placeholder="Leave blank for personal repo",
        required=False,
        max_length=100,
    )

    def __init__(self, handler, bot, *, default_name: str = "") -> None:
        super().__init__()
        self._handler = handler
        self._bot = bot
        if default_name:
            self.repo_name_input.default = default_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state = _get_or_create_state(interaction.user.id)
        state.create_repo = True
        state.repo_name = self.repo_name_input.value.strip() or state.project_id
        # Normalize visibility
        vis = self.visibility_input.value.strip().lower()
        state.repo_visibility = vis if vis in ("public", "private") else "private"
        state.repo_org = self.org_input.value.strip()

        embed = _state_summary_embed(state)
        embed.add_field(
            name="Workspaces",
            value="How many workspaces for parallel agent execution?",
            inline=False,
        )
        view = WorkspaceCountView(self._handler, self._bot)
        await interaction.response.edit_message(embed=embed, view=view)


# ---------------------------------------------------------------------------
# Modal 2b: Existing Repository URL
# ---------------------------------------------------------------------------


class ExistingRepoModal(discord.ui.Modal, title="Existing Repository"):
    """Single text input to provide an existing repo URL."""

    repo_url_input = discord.ui.TextInput(
        label="Repository URL",
        placeholder="https://github.com/user/repo.git",
        required=True,
        max_length=500,
    )

    def __init__(self, handler, bot) -> None:
        super().__init__()
        self._handler = handler
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state = _get_or_create_state(interaction.user.id)
        state.repo_url = self.repo_url_input.value.strip()
        state.create_repo = False

        embed = _state_summary_embed(state)
        embed.add_field(
            name="Workspaces",
            value="How many workspaces for parallel agent execution?",
            inline=False,
        )
        view = WorkspaceCountView(self._handler, self._bot)
        await interaction.response.edit_message(embed=embed, view=view)


# ---------------------------------------------------------------------------
# View: Workspace count selection
# ---------------------------------------------------------------------------


class WorkspaceCountView(discord.ui.View):
    """Buttons for selecting the number of workspaces (2-5)."""

    def __init__(self, handler, bot) -> None:
        super().__init__(timeout=_WIZARD_TIMEOUT_SECONDS)
        self._handler = handler
        self._bot = bot

        for count in (2, 3, 4, 5):
            btn = discord.ui.Button(
                label=str(count),
                style=discord.ButtonStyle.primary if count == 3 else discord.ButtonStyle.secondary,
                row=0,
            )
            btn.callback = self._make_count_callback(count)
            self.add_item(btn)

    def _make_count_callback(self, count: int):
        async def callback(interaction: discord.Interaction) -> None:
            state = _get_or_create_state(interaction.user.id)
            state.workspace_count = count

            embed = _state_summary_embed(state)
            embed.add_field(
                name="Workspace Location",
                value=(
                    "Where should workspaces be cloned?\n"
                    "Use the default location or specify a custom path."
                ),
                inline=False,
            )
            view = WorkspaceLocationView(self._handler, self._bot)
            await interaction.response.edit_message(embed=embed, view=view)

        return callback

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def cancel_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        _cleanup_state(interaction.user.id)
        await interaction.response.edit_message(
            content="Project wizard cancelled.",
            embed=None,
            view=None,
        )


# ---------------------------------------------------------------------------
# View: Workspace location (Default / Custom)
# ---------------------------------------------------------------------------


class WorkspaceLocationView(discord.ui.View):
    """Buttons to choose default or custom workspace clone location."""

    def __init__(self, handler, bot) -> None:
        super().__init__(timeout=_WIZARD_TIMEOUT_SECONDS)
        self._handler = handler
        self._bot = bot

    @discord.ui.button(
        label="Use Default Location",
        style=discord.ButtonStyle.primary,
        emoji="\U0001f4c1",
        row=0,
    )
    async def default_location_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        state = _get_or_create_state(interaction.user.id)
        state.workspace_root = ""  # Empty = use default
        await _execute_wizard(interaction, state, self._handler, self._bot)

    @discord.ui.button(
        label="Custom Location",
        style=discord.ButtonStyle.secondary,
        emoji="\U0001f4dd",
        row=0,
    )
    async def custom_location_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        modal = WorkspaceLocationModal(self._handler, self._bot)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def cancel_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        _cleanup_state(interaction.user.id)
        await interaction.response.edit_message(
            content="Project wizard cancelled.",
            embed=None,
            view=None,
        )


# ---------------------------------------------------------------------------
# Modal: Custom workspace location
# ---------------------------------------------------------------------------


class WorkspaceLocationModal(discord.ui.Modal, title="Workspace Location"):
    """Collect a custom path for workspace cloning."""

    location_input = discord.ui.TextInput(
        label="Workspace Root Directory",
        placeholder="/home/user/projects/my-app-workspaces",
        style=discord.TextStyle.short,
        required=True,
        max_length=500,
    )

    def __init__(self, handler, bot) -> None:
        super().__init__()
        self._handler = handler
        self._bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        state = _get_or_create_state(interaction.user.id)
        state.workspace_root = self.location_input.value.strip()

        if not state.workspace_root:
            await interaction.response.send_message(
                "Workspace location is required when using a custom path.",
                ephemeral=True,
            )
            return

        await _execute_wizard(interaction, state, self._handler, self._bot)


# ---------------------------------------------------------------------------
# Wizard Executor
# ---------------------------------------------------------------------------


class WizardError(Exception):
    """Raised when a wizard step fails and rollback is needed."""


async def _execute_wizard(
    interaction: discord.Interaction,
    state: ProjectWizardState,
    handler,
    bot,
) -> None:
    """Execute all project creation steps, with rollback on failure.

    Steps:
        1. Create GitHub repo (if requested)
        2. Create project via command handler
        3. Clone N workspaces
        4. Create Discord channel (auto-create)
        5. Generate and commit README

    On failure, previously completed steps are rolled back.
    """
    await interaction.response.defer(ephemeral=True)

    created_project = False
    created_workspaces: list[str] = []

    try:
        # -- Progress embed --
        progress = discord.Embed(
            title="Setting up project...",
            description="This may take a moment.",
            color=0xF39C12,
        )
        await interaction.followup.send(embed=progress, ephemeral=True)

        # Step 1: Create GitHub repo (if requested)
        if state.create_repo:
            progress.description = "Creating GitHub repository..."
            try:
                await interaction.edit_original_response(embed=progress)
            except discord.NotFound:
                pass

            result = await handler.execute(
                "create_github_repo",
                {
                    "name": state.repo_name or state.project_id,
                    "visibility": state.repo_visibility,
                    "org": state.repo_org,
                },
            )
            if "error" in result:
                raise WizardError(f"GitHub repo creation failed: {result['error']}")
            state.repo_url = result.get("repo_url", "")

        # Step 2: Create project
        progress.description = "Creating project..."
        try:
            await interaction.edit_original_response(embed=progress)
        except discord.NotFound:
            pass

        create_args: dict = {
            "name": state.project_name,
            "repo_url": state.repo_url,
            "default_branch": state.default_branch,
            "auto_create_channels": True,
        }
        result = await handler.execute("create_project", create_args)
        if "error" in result:
            raise WizardError(f"Project creation failed: {result['error']}")
        created_project = True
        project_id = result.get("created", state.project_id)

        # Step 3: Clone workspaces
        for i in range(state.workspace_count):
            progress.description = f"Creating workspace {i + 1}/{state.workspace_count}..."
            try:
                await interaction.edit_original_response(embed=progress)
            except discord.NotFound:
                pass

            ws_args: dict = {
                "project_id": project_id,
                "source": "clone" if state.repo_url else "init",
            }
            # If a custom workspace root was specified, build the path
            if state.workspace_root:
                import uuid as _uuid

                ws_name = f"checkout-{_uuid.uuid4().hex[:6]}"
                ws_args["path"] = os.path.join(
                    state.workspace_root,
                    project_id,
                    ws_name,
                )
            result = await handler.execute("add_workspace", ws_args)
            if "error" in result:
                raise WizardError(f"Workspace {i + 1} creation failed: {result['error']}")
            ws_id = result.get("created")
            if ws_id:
                created_workspaces.append(ws_id)

        # Step 4: Auto-create Discord channel (private, with bot access)
        channel_info = ""
        if interaction.guild is not None:
            progress.description = "Creating Discord channel..."
            try:
                await interaction.edit_original_response(embed=progress)
            except discord.NotFound:
                pass

            guild = interaction.guild
            ppc = bot.config.discord.per_project_channels
            channel_name = ppc.naming_convention.format(project_id=project_id)

            # Look up optional category
            target_category = None
            if ppc.category_name:
                target_category = discord.utils.get(guild.categories, name=ppc.category_name)

            # Make the channel private: deny @everyone, allow the bot
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_messages=True,
                ),
            }

            try:
                new_channel = await guild.create_text_channel(
                    name=channel_name,
                    category=target_category,
                    topic=f"Agent Queue channel for project: {project_id}",
                    overwrites=overwrites,
                    reason=f"AgentQueue: channel for project {project_id}",
                )

                # Link channel to project in the database
                await handler.execute(
                    "set_project_channel",
                    {
                        "project_id": project_id,
                        "channel_id": str(new_channel.id),
                    },
                )

                # Update bot's in-memory channel cache
                bot.update_project_channel(project_id, new_channel)
                channel_info = new_channel.mention
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "Failed to create Discord channel for %s: %s",
                    project_id,
                    exc,
                )
                channel_info = "(channel creation failed)"

        # Step 5: Generate README (if we have a repo)
        if state.repo_url and created_workspaces:
            progress.description = "Generating README..."
            try:
                await interaction.edit_original_response(embed=progress)
            except discord.NotFound:
                pass

            readme_args: dict = {
                "project_id": project_id,
                "name": state.project_name,
                "description": state.description,
                "tech_stack": state.tech_stack,
            }
            readme_result = await handler.execute("generate_readme", readme_args)
            # README generation is non-fatal -- log but don't fail the wizard
            if "error" in readme_result:
                logger.warning(
                    "README generation failed for %s: %s",
                    project_id,
                    readme_result["error"],
                )

        # -- Success embed --
        embed = success_embed(
            title="Project Setup Complete!",
            description=f"**{state.project_name}** is ready to go.",
        )
        embed.add_field(name="Project ID", value=f"`{project_id}`", inline=True)
        if state.repo_url:
            embed.add_field(name="Repository", value=state.repo_url, inline=False)
        ws_info = f"{len(created_workspaces)} workspace(s) created"
        if state.workspace_root:
            ws_info += f"\nLocation: `{state.workspace_root}`"
        embed.add_field(
            name="Workspaces",
            value=ws_info,
            inline=True,
        )
        if channel_info:
            embed.add_field(name="Channel", value=channel_info, inline=True)
        if state.description:
            desc_preview = state.description[:200]
            if len(state.description) > 200:
                desc_preview += "..."
            embed.add_field(name="Description", value=desc_preview, inline=False)
        if state.tech_stack:
            embed.add_field(name="Tech Stack", value=state.tech_stack, inline=True)

        embed.set_footer(text="You're ready to create tasks!")

        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.NotFound:
            await interaction.followup.send(embed=embed, ephemeral=True)

    except WizardError as e:
        logger.error("Wizard failed for user %s: %s", state.user_id, e)

        # -- Rollback --
        for ws_id in reversed(created_workspaces):
            try:
                await handler.execute(
                    "remove_workspace",
                    {
                        "workspace_id": ws_id,
                    },
                )
            except Exception:
                logger.warning("Rollback: failed to remove workspace %s", ws_id)

        if created_project:
            try:
                await handler.execute(
                    "delete_project",
                    {
                        "project_id": state.project_id,
                    },
                )
            except Exception:
                logger.warning(
                    "Rollback: failed to delete project %s",
                    state.project_id,
                )

        embed = error_embed(
            title="Project Setup Failed",
            description=str(e),
        )
        embed.add_field(
            name="What happened",
            value="All changes have been rolled back. You can try again.",
            inline=False,
        )
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.NotFound:
            await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        logger.exception("Unexpected error in wizard executor for user %s", state.user_id)

        # Best-effort rollback
        for ws_id in reversed(created_workspaces):
            try:
                await handler.execute(
                    "remove_workspace",
                    {
                        "workspace_id": ws_id,
                    },
                )
            except Exception:
                pass
        if created_project:
            try:
                await handler.execute(
                    "delete_project",
                    {
                        "project_id": state.project_id,
                    },
                )
            except Exception:
                pass

        embed = error_embed(
            title="Project Setup Failed",
            description=f"An unexpected error occurred: {e}",
        )
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.NotFound:
            await interaction.followup.send(embed=embed, ephemeral=True)

    finally:
        _cleanup_state(state.user_id)
