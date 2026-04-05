#!/usr/bin/env python3
"""Interactive setup wizard for agent-queue.

Walks the user through first-time configuration: Discord bot token, API keys,
default project creation, and agent provisioning.  Run automatically by
``setup.sh`` or manually via ``python -m src.setup_wizard``.

The wizard follows a linear multi-step flow:

1. **Directories** — workspace and database paths.
2. **Discord** — bot token, guild ID, channel names, connectivity test.
3. **Claude Code** — agent binary detection, API key or local model.
4. **Chat provider** — which LLM backend the chat bot uses.
5. **Scheduling & budget** — scheduling interval and optional token budget.
6. **Config generation** — writes ``~/.agent-queue/config.yaml``.

All secrets are stored in ``~/.agent-queue/.env`` (mode 0600) rather than
in the YAML config.  The wizard is idempotent — it detects existing config
values and pre-fills them, only prompting for missing fields.

See ``specs/setup.md`` for the configuration specification.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import subprocess
import sys
from pathlib import Path

# ANSI colors
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"
RESET = "\033[0m"


def banner() -> None:
    """Print the setup wizard banner with ANSI styling."""
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════╗
║        agent-queue setup wizard      ║
╚══════════════════════════════════════╝{RESET}
""")


def step_header(num: int, title: str) -> None:
    """Print a numbered step header with ANSI styling.

    Args:
        num: Step number.
        title: Step title text.
    """
    print(f"\n{BOLD}{CYAN}── Step {num}: {title} ──{RESET}\n")


def success(msg: str) -> None:
    """Print a green success message."""
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    """Print a yellow warning message."""
    print(f"  {YELLOW}!{RESET} {msg}")


def error(msg: str) -> None:
    """Print a red error message."""
    print(f"  {RED}✗{RESET} {msg}")


def info(msg: str) -> None:
    """Print a dim informational message."""
    print(f"  {DIM}{msg}{RESET}")


def prompt(label: str, default: str = "") -> str:
    """Prompt the user for text input with an optional default.

    Args:
        label: Prompt label shown to the user.
        default: Default value shown in brackets; returned when input is empty.

    Returns:
        User's input, or *default* if empty.
    """
    suffix = f" [{default}]" if default else ""
    value = input(f"  {label}{suffix}: ").strip()
    return value or default


def prompt_yes_no(label: str, default: bool = True) -> bool:
    """Prompt the user for a yes/no answer.

    Args:
        label: Prompt label shown to the user.
        default: Default value when input is empty.

    Returns:
        ``True`` for yes, ``False`` for no.
    """
    hint = "Y/n" if default else "y/N"
    value = input(f"  {label} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def prompt_secret(label: str, existing: str = "") -> str:
    """Prompt for a secret value (hidden input), showing masked existing value.

    Args:
        label: Prompt label.
        existing: Previously saved value (shown masked).

    Returns:
        New secret, or *existing* if input is empty.
    """
    if existing:
        masked = existing[:4] + "..." + existing[-4:] if len(existing) > 12 else "****"
        value = input(f"  {label} [{masked}]: ").strip()
        return value if value else existing
    return getpass.getpass(f"  {label} (input is hidden): ").strip()


def _save_env_value(key: str, value: str):
    """Incrementally save a key=value pair to ~/.agent-queue/.env.

    Updates existing keys in place, appends new ones.
    Skips saving if value is empty.
    """
    if not value:
        return

    config_dir = Path(os.path.expanduser("~/.agent-queue"))
    config_dir.mkdir(parents=True, exist_ok=True)
    env_path = config_dir / ".env"

    lines: list[str] = []
    found = False
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)


# ── Load existing config ────────────────────────────────────────────────────


def _load_existing_config() -> dict:
    """Load existing config.yaml and .env into a dict for pre-filling prompts."""
    existing: dict = {}
    config_dir = Path(os.path.expanduser("~/.agent-queue"))

    # Load .env
    env_path = config_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()

    # Load config.yaml (simple key-value parsing, no PyYAML dependency)
    config_path = config_dir / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            existing["_yaml"] = data
        except ImportError:
            # Fall back to simple line parsing
            data: dict = {}
            current_section: str | None = None
            current_subsection: str | None = None
            for line in config_path.read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                if indent == 0 and ":" in stripped:
                    key, _, val = stripped.partition(":")
                    val = val.strip()
                    if val:
                        data[key.strip()] = val.strip('"').strip("'")
                    else:
                        current_section = key.strip()
                        current_subsection = None
                        data.setdefault(current_section, {})
                elif indent > 0 and current_section and ":" in stripped:
                    key, _, val = stripped.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if val:
                        if isinstance(data[current_section], dict):
                            data[current_section][key] = val.strip('"').strip("'")
                    else:
                        current_subsection = key
                        if isinstance(data[current_section], dict):
                            data[current_section].setdefault(current_subsection, {})
                    if current_subsection and val and isinstance(data[current_section], dict):
                        subsec = data[current_section].get(current_subsection)
                        if isinstance(subsec, dict):
                            subsec[key] = val.strip('"').strip("'")
            existing["_yaml"] = data

    return existing


# ── Step 1: Directories ──────────────────────────────────────────────────────


def step_directories(existing: dict) -> str:
    """Step 1a: Configure workspace directory.

    Args:
        existing: Pre-loaded config values from ``_load_existing_config()``.

    Returns:
        Workspace directory path.
    """
    yaml_cfg = existing.get("_yaml", {})
    default_workspace = (
        existing.get("WORKSPACE_DIR")
        or yaml_cfg.get("workspace_dir")
        or os.path.expanduser("~/agent-queue-workspaces")
    )

    has_workspace = existing.get("WORKSPACE_DIR") or yaml_cfg.get("workspace_dir")
    if has_workspace or os.path.isdir(os.path.expanduser(default_workspace)):
        workspace = os.path.expanduser(default_workspace)
        os.makedirs(workspace, exist_ok=True)
        success(f"Workspace: {workspace}")
        return workspace

    step_header(1, "Workspace Directory")

    workspace = prompt("Workspace directory", default_workspace)
    workspace = os.path.expanduser(workspace)
    _save_env_value("WORKSPACE_DIR", workspace)
    os.makedirs(workspace, exist_ok=True)
    success(f"Directory ready: {workspace}")

    return workspace


def _check_pg_running(host: str = "localhost", port: int = 5533) -> bool:
    """Check if PostgreSQL is reachable on the given host:port."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _find_docker_compose() -> str | None:
    """Find docker-compose.yml in the project repo (if installed from source)."""
    # Check relative to this file (src/setup_wizard.py → project root)
    project_root = Path(__file__).resolve().parent.parent
    compose_file = project_root / "docker-compose.yml"
    if compose_file.exists():
        return str(compose_file)
    return None


def _boot_docker_postgres(compose_file: str) -> bool:
    """Start the PostgreSQL container via docker compose."""
    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_file, "up", "-d", "postgres"],
            check=True,
            capture_output=True,
            text=True,
        )
        success("PostgreSQL container started")
        # Wait for it to be ready
        info("Waiting for PostgreSQL to be ready...")
        for _ in range(30):
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    compose_file,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_isready",
                    "-U",
                    "agent_queue",
                ],
                capture_output=True,
            )
            if result.returncode == 0:
                success("PostgreSQL is ready")
                return True
            import time

            time.sleep(1)
        error("PostgreSQL did not become ready in time")
        return False
    except FileNotFoundError:
        error("docker or docker compose not found")
        return False
    except subprocess.CalledProcessError as e:
        error(f"Failed to start PostgreSQL: {e.stderr}")
        return False


def _ensure_asyncpg() -> bool:
    """Ensure asyncpg is installed, offering to install it if missing."""
    try:
        import asyncpg  # noqa: F401

        return True
    except ImportError:
        info("asyncpg (PostgreSQL driver) is not installed")
        install = prompt_yes_no("Install it now?")
        if install:
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "asyncpg>=0.29.0"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                success("asyncpg installed")
                return True
            except subprocess.CalledProcessError as e:
                error(f"Failed to install asyncpg: {e.stderr}")
                return False
        error("asyncpg is required for PostgreSQL. Install with: pip install asyncpg")
        return False


def _test_pg_dsn(dsn: str) -> bool:
    """Test PostgreSQL connectivity using asyncpg."""
    try:
        import asyncpg
    except ImportError:
        error("asyncpg not installed")
        return False

    async def _connect():
        conn = await asyncpg.connect(dsn)
        await conn.close()

    try:
        asyncio.run(_connect())
        return True
    except Exception as e:
        error(f"Connection failed: {e}")
        return False


def _select_postgresql(existing_sqlite_path: str | None = None) -> dict:
    """Interactive PostgreSQL selection sub-flow.

    Returns:
        Dict with ``backend``, ``url``, ``pool_min_size``, ``pool_max_size``.
    """
    if not _ensure_asyncpg():
        raise SystemExit(1)

    default_dsn = "postgresql://agent_queue:agent_queue_dev@localhost:5533/agent_queue"

    if _check_pg_running():
        info("PostgreSQL detected on localhost:5533")
        use_local = prompt_yes_no("Use this PostgreSQL instance?")
        if use_local:
            dsn = prompt("PostgreSQL DSN", default_dsn)
            if _test_pg_dsn(dsn):
                success("Connected to PostgreSQL")
                return _build_pg_config(dsn, existing_sqlite_path)
            else:
                warn("Could not connect. Enter a different DSN or check credentials.")
                dsn = prompt("PostgreSQL DSN", dsn)
                if not _test_pg_dsn(dsn):
                    error("Still cannot connect. Aborting PostgreSQL setup.")
                    raise SystemExit(1)
                return _build_pg_config(dsn, existing_sqlite_path)

    # No PG running — try Docker
    compose_file = _find_docker_compose()
    if compose_file:
        # Check if docker is available
        docker_available = subprocess.run(["docker", "info"], capture_output=True).returncode == 0

        if docker_available:
            info("No PostgreSQL running, but docker-compose.yml found")
            boot = prompt_yes_no("Start PostgreSQL via Docker?")
            if boot:
                if _boot_docker_postgres(compose_file):
                    if _test_pg_dsn(default_dsn):
                        success("Connected to Docker PostgreSQL")
                        return _build_pg_config(default_dsn, existing_sqlite_path)
                    else:
                        error("Container started but connection failed")

    # Manual DSN entry
    print()
    info("Enter your PostgreSQL connection DSN:")
    info("  Format: postgresql://user:password@host:port/dbname")
    dsn = prompt("PostgreSQL DSN")
    if not dsn:
        error("No DSN provided. Aborting.")
        raise SystemExit(1)
    if _test_pg_dsn(dsn):
        success("Connected to PostgreSQL")
        return _build_pg_config(dsn, existing_sqlite_path)
    else:
        error("Cannot connect to PostgreSQL. Check your DSN and try again.")
        raise SystemExit(1)


def _build_pg_config(dsn: str, existing_sqlite_path: str | None) -> dict:
    """Build PG config dict and optionally migrate SQLite data."""
    config = {
        "backend": "postgresql",
        "url": dsn,
        "pool_min_size": 2,
        "pool_max_size": 10,
    }

    if existing_sqlite_path and os.path.exists(existing_sqlite_path):
        print()
        info(f"Existing SQLite database found at: {existing_sqlite_path}")
        migrate = prompt_yes_no("Migrate existing data to PostgreSQL?")
        if migrate:
            config["_migrate_from"] = existing_sqlite_path

    return config


def step_database(existing: dict) -> dict:
    """Step 1b: Configure database backend.

    Handles re-run detection: if an existing config specifies PostgreSQL,
    confirms it. If SQLite, offers to switch. If no config, prompts for choice.

    Args:
        existing: Pre-loaded config values from ``_load_existing_config()``.

    Returns:
        Dict with ``backend``, ``url``, and optionally ``pool_min_size``,
        ``pool_max_size``, ``_migrate_from``.
    """
    yaml_cfg = existing.get("_yaml", {})
    db_section = yaml_cfg.get("database", {})
    existing_url = db_section.get("url", "") if isinstance(db_section, dict) else ""
    existing_sqlite = existing.get("DATABASE_PATH") or yaml_cfg.get("database_path") or ""

    # Re-run: existing PostgreSQL config
    if existing_url.startswith(("postgresql://", "postgres://")):
        success(
            f"Database: PostgreSQL ({existing_url.split('@')[-1] if '@' in existing_url else existing_url})"
        )
        keep = prompt_yes_no("Keep current PostgreSQL configuration?")
        if keep:
            return {
                "backend": "postgresql",
                "url": existing_url,
                "pool_min_size": db_section.get("pool_min_size", 2),
                "pool_max_size": db_section.get("pool_max_size", 10),
            }
        # Fall through to re-select

    # Re-run: existing SQLite config
    elif existing_sqlite:
        default_db = os.path.expanduser(existing_sqlite)
        # Check if this is just defaults that already exist
        has_saved_db = existing.get("DATABASE_PATH") or yaml_cfg.get("database_path")
        defaults_exist = os.path.isdir(os.path.dirname(default_db))
        if has_saved_db or defaults_exist:
            success(f"Database: SQLite ({default_db})")
            switch = prompt_yes_no("Switch to PostgreSQL?", default=False)
            if not switch:
                os.makedirs(os.path.dirname(default_db), exist_ok=True)
                return {"backend": "sqlite", "url": default_db}
            return _select_postgresql(existing_sqlite_path=default_db)

    # Fresh install
    step_header(1, "Database Backend")

    print(f"  {BOLD}Database options:{RESET}")
    print(f"    1) SQLite {DIM}(default — zero config, file-based){RESET}")
    print(f"    2) PostgreSQL {DIM}(recommended for production){RESET}")
    print()
    choice = prompt("Select database backend", "1")

    if choice == "2":
        return _select_postgresql()

    # SQLite
    default_db = os.path.expanduser("~/.agent-queue/agent-queue.db")
    db_path = prompt("Database path", default_db)
    db_path = os.path.expanduser(db_path)
    _save_env_value("DATABASE_PATH", db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    success(f"Directory ready: {os.path.dirname(db_path)}")
    return {"backend": "sqlite", "url": db_path}


# ── Step 2: Discord ──────────────────────────────────────────────────────────


def step_discord(existing: dict) -> dict:
    """Step 2: Configure Discord bot token, guild, and channels.

    Collects the bot token and guild ID (prompting if not already saved),
    tests connectivity, and returns the Discord configuration dict.

    Args:
        existing: Pre-loaded config values.

    Returns:
        Dict with ``token``, ``guild_id``, and ``channels`` keys.
    """
    yaml_cfg = existing.get("_yaml", {})
    discord_cfg = yaml_cfg.get("discord", {})
    existing_channels = discord_cfg.get("channels", {})

    existing_token = existing.get("DISCORD_BOT_TOKEN", "")
    existing_guild = discord_cfg.get("guild_id", "") or existing.get("DISCORD_GUILD_ID", "")

    # Collect token if not saved
    if existing_token:
        bot_token = existing_token
    else:
        step_header(2, "Discord Bot")
        print(f"""  To create a Discord bot:
  1. Go to {BOLD}https://discord.com/developers/applications{RESET}
  2. Click {BOLD}New Application{RESET}, give it a name, and create it
  3. Go to the {BOLD}Bot{RESET} tab and click {BOLD}Reset Token{RESET} to get your bot token
  4. Under {BOLD}Privileged Gateway Intents{RESET}, enable {BOLD}Message Content Intent{RESET}
  5. Go to {BOLD}OAuth2 > URL Generator{RESET}:
     - Scopes: {BOLD}bot{RESET}, {BOLD}applications.commands{RESET}
     - Bot Permissions: {BOLD}Send Messages{RESET}, {BOLD}Read Message History{RESET}, {BOLD}Use Slash Commands{RESET}
  6. Copy the generated URL, open it in your browser, and add the bot to your server
""")
        bot_token = prompt_secret("Bot token")
        if not bot_token:
            error("Bot token is required")
            sys.exit(1)
        _save_env_value("DISCORD_BOT_TOKEN", bot_token)

    # Collect guild ID if not saved
    if existing_guild:
        guild_id = existing_guild
    else:
        print(f"""
  To find your Guild (server) ID:
  1. Open Discord Settings > {BOLD}Advanced{RESET} > enable {BOLD}Developer Mode{RESET}
  2. Right-click your server name and click {BOLD}Copy Server ID{RESET}
""")
        guild_id = prompt("Guild ID")
        if not guild_id:
            error("Guild ID is required")
            sys.exit(1)
        _save_env_value("DISCORD_GUILD_ID", guild_id)

    # Channel names (before connectivity test)
    channels = {
        "control": existing_channels.get("control", "control"),
        "notifications": existing_channels.get("notifications", "notifications"),
        "agent_questions": existing_channels.get("agent_questions", "agent-questions"),
    }

    # Test connectivity with retry loop (including channel verification)
    print()
    info("Testing Discord connectivity...")
    discord_ok = False
    while True:
        discord_ok, missing_channels = _test_discord(bot_token, guild_id, channels)
        if discord_ok:
            break

        if missing_channels:
            # Bot connected fine, just channels are wrong — offer to customize
            print()
            warn("Bot connected successfully, but some channels weren't found.")
            info("Either create them in Discord, or update the names here.")
            print()
            if prompt_yes_no("Update channel names?", default=True):
                channels["control"] = prompt("Control channel", channels["control"])
                channels["notifications"] = prompt(
                    "Notifications channel", channels["notifications"]
                )
                channels["agent_questions"] = prompt(
                    "Agent questions channel", channels["agent_questions"]
                )
                info("Re-testing with updated channels...")
                continue
            elif not prompt_yes_no("Retry with current channel names?", default=True):
                break
        else:
            # Connection-level failure
            print()
            warn("Discord connection failed. Debugging tips:")
            info("  - Verify the bot token is correct (reset it at discord.com/developers)")
            info("  - Check that the bot has been invited to the server")
            info("  - Ensure Message Content Intent is enabled under Privileged Gateway Intents")
            info(f"  - Confirm guild ID {guild_id} matches your server")
            print()
            choice = prompt("(R)etry / (C)onfigure new bot+server / (S)kip", "R")
            choice = choice.strip().lower()
            if choice in ("c", "configure"):
                step_header(2, "Discord Bot (Reconfigure)")
                print(f"""  To create or reconfigure a Discord bot:
  1. Go to {BOLD}https://discord.com/developers/applications{RESET}
  2. Click {BOLD}New Application{RESET} (or select existing), go to {BOLD}Bot{RESET} tab
  3. Click {BOLD}Reset Token{RESET} to get your bot token
  4. Under {BOLD}Privileged Gateway Intents{RESET}, enable {BOLD}Message Content Intent{RESET}
  5. Go to {BOLD}OAuth2 > URL Generator{RESET}:
     - Scopes: {BOLD}bot{RESET}, {BOLD}applications.commands{RESET}
     - Bot Permissions: {BOLD}Send Messages{RESET}, {BOLD}Read Message History{RESET}, {BOLD}Use Slash Commands{RESET}
  6. Copy the generated URL, open it in your browser, and add the bot to your server
""")
                new_token = prompt_secret("Bot token")
                if new_token:
                    bot_token = new_token
                    _save_env_value("DISCORD_BOT_TOKEN", bot_token)
                print(f"""
  To find your Guild (server) ID:
  1. Open Discord Settings > {BOLD}Advanced{RESET} > enable {BOLD}Developer Mode{RESET}
  2. Right-click your server name and click {BOLD}Copy Server ID{RESET}
""")
                new_guild = prompt("Guild ID", guild_id)
                if new_guild:
                    guild_id = new_guild
                    _save_env_value("DISCORD_GUILD_ID", guild_id)
                info("Re-testing with new credentials...")
                continue
            elif choice in ("r", "retry"):
                continue
            else:
                break

    # Authorized users
    authorized_users: list[str] = []
    if prompt_yes_no("Restrict commands to specific Discord user IDs?", default=False):
        print("  Enter user IDs one per line (empty line to finish):")
        while True:
            uid = input("    > ").strip()
            if not uid:
                break
            authorized_users.append(uid)

    # Per-project channel configuration
    per_project_cfg = _step_per_project_channels(existing, discord_ok)

    return {
        "bot_token": bot_token,
        "guild_id": guild_id,
        "channels": channels,
        "authorized_users": authorized_users,
        "connected": discord_ok,
        "per_project_channels": per_project_cfg,
    }


def _step_per_project_channels(existing: dict, discord_ok: bool) -> dict:
    """Guide users through per-project Discord channel configuration.

    Returns a dict with per-project channel settings:
        auto_create: bool — auto-create channels when projects are created
        naming_convention: str — channel name pattern
        category_name: str — Discord category name for project channels
    """
    yaml_cfg = existing.get("_yaml", {})
    discord_cfg = yaml_cfg.get("discord", {})
    existing_ppc = discord_cfg.get("per_project_channels", {})

    defaults = {
        "auto_create": existing_ppc.get("auto_create", False),
        "naming_convention": existing_ppc.get("naming_convention", "{project_id}"),
        "category_name": existing_ppc.get("category_name", ""),
        "private": existing_ppc.get("private", True),
    }

    print()
    print(f"  {BOLD}Per-Project Channels{RESET}")
    info("Each project can have its own dedicated Discord channel")
    info("instead of sharing the global channel.")
    print()

    if not prompt_yes_no(
        "Enable automatic per-project channel creation?",
        default=defaults["auto_create"],
    ):
        # User declined — show manual instructions and return defaults (disabled)
        if discord_ok:
            print()
            info("You can still create per-project channels manually via Discord:")
            info("  /create-channel <project-id>  — create & link a new channel")
            info("  /set-channel <project-id>     — link an existing channel")
            info("  /channel-map                  — view all project-channel mappings")
            info("Projects without dedicated channels fall back to the global channels.")
        return {
            "auto_create": False,
            "naming_convention": defaults["naming_convention"],
            "category_name": defaults["category_name"],
            "private": defaults["private"],
        }

    # ── Naming convention ──
    print()
    info("Channel naming convention uses {project_id} as a placeholder.")
    info("Examples: for a project 'my-app',")
    info("  '{project_id}'       →  #my-app")
    info("  'aq-{project_id}'    →  #aq-my-app")
    print()

    naming_convention = prompt(
        "Channel name pattern",
        defaults["naming_convention"],
    )
    if "{project_id}" not in naming_convention:
        warn("Pattern must contain {project_id} — resetting to default")
        naming_convention = "{project_id}"

    # ── Category ──
    print()
    info("You can organize project channels under a Discord category.")
    info("If the category doesn't exist, it will be created automatically.")
    print()

    category_name = prompt(
        "Discord category for project channels (blank to skip)",
        defaults["category_name"],
    )

    # ── Private channels ──
    print()
    info("Private channels are only visible to the bot and users you grant access.")
    private = prompt_yes_no(
        "Make project channels private?",
        default=defaults["private"],
    )

    # ── Summary ──
    print()
    success("Per-project channel configuration:")
    info("  Auto-create:         enabled")
    info(f"  Channel pattern:     {naming_convention}")
    if category_name:
        info(f"  Category:            {category_name}")
    else:
        info("  Category:            (none — channels created at top level)")
    info(f"  Private:             {'yes' if private else 'no'}")

    if discord_ok:
        print()
        info("Channels will be created automatically when you add projects.")
        info("You can also manage them manually:")
        info("  /create-channel <project-id>  — create & link a channel")
        info("  /set-channel <project-id>     — link an existing channel")
        info("  /channel-map                  — view all project-channel mappings")

    return {
        "auto_create": True,
        "naming_convention": naming_convention,
        "category_name": category_name,
        "private": private,
    }


def _test_discord(
    token: str, guild_id: str, channels: dict | None = None
) -> tuple[bool, list[str]]:
    """Test Discord bot connectivity and verify channels exist in the guild.

    Returns (success, missing_channel_names).
    """
    try:
        # Import discord.py library, not our local src/discord/ package.
        # When running as `python src/setup_wizard.py`, src/ is on sys.path
        # and would shadow the real discord.py library.
        import importlib
        import sys

        _src = str(Path(__file__).resolve().parent)
        _patched = _src in sys.path
        if _patched:
            sys.path.remove(_src)
        discord = importlib.import_module("discord")
        if _patched:
            sys.path.insert(0, _src)

        result: dict = {"ok": False, "connected": False, "missing_channels": []}

        async def _check():
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)

            @client.event
            async def on_ready():
                guild = client.get_guild(int(guild_id))
                if guild:
                    success(f"Connected to Discord — guild: {guild.name}")
                    result["connected"] = True
                    result["ok"] = True

                    # Verify channels exist
                    if channels:
                        guild_channel_names = {ch.name for ch in guild.text_channels}
                        for key, channel_name in channels.items():
                            if channel_name not in guild_channel_names:
                                result["missing_channels"].append(channel_name)

                        if result["missing_channels"]:
                            result["ok"] = False
                            for missing in result["missing_channels"]:
                                error(f"Channel not found: #{missing}")
                        else:
                            success("All configured channels found")
                else:
                    error(f"Bot connected but cannot see guild {guild_id}")
                    warn("Make sure the bot has been invited to the server")
                await client.close()

            try:
                await asyncio.wait_for(client.start(token), timeout=15)
            except asyncio.TimeoutError:
                error("Discord connection timed out")
            except discord.LoginFailure:
                error("Invalid bot token")
            except discord.PrivilegedIntentsRequired:
                error("Privileged intents not enabled")
                print()
                warn("You need to enable Message Content Intent in the Discord Developer Portal:")
                info(f"  1. Go to {BOLD}https://discord.com/developers/applications/{RESET}")
                info("  2. Select your bot application")
                info(f"  3. Go to the {BOLD}Bot{RESET} tab")
                info(f"  4. Scroll to {BOLD}Privileged Gateway Intents{RESET}")
                info(f"  5. Enable {BOLD}Message Content Intent{RESET}")
                info(f"  6. Click {BOLD}Save Changes{RESET}")
            except Exception as e:
                error(f"Discord error: {e}")
            finally:
                if not client.is_closed():
                    await client.close()

        asyncio.run(_check())
        return result["ok"], result["missing_channels"]
    except ImportError:
        warn("discord.py not installed — skipping connectivity test")
        return False, []


# ── Step 3: Agent Configuration ──────────────────────────────────────────────


def _check_claude_cli() -> tuple[bool, bool]:
    """Check if the claude CLI is installed and has credentials.

    Returns (is_installed, is_authenticated).
    is_authenticated is True if credentials from `claude login` are present.
    """
    import shutil
    from pathlib import Path

    if not shutil.which("claude"):
        return False, False

    # Claude Code stores OAuth credentials after `claude login`
    home = Path.home()
    for cred_path in [
        home / ".claude" / ".credentials.json",
        home / ".claude" / "credentials.json",
    ]:
        if cred_path.exists():
            return True, True

    return True, False


def step_agents(existing: dict) -> dict:
    step_header(3, "Agent Configuration")

    yaml_cfg = existing.get("_yaml", {})

    print("  Available agent types:")
    print(f"    {BOLD}[1] Claude Code{RESET}  (supported)")
    print(f"    {DIM}[2] Codex        (not yet implemented){RESET}")
    print(f"    {DIM}[3] Cursor       (not yet implemented){RESET}")
    print(f"    {DIM}[4] Aider        (not yet implemented){RESET}")
    print()

    agents: dict = {"claude": None}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "") or existing.get("ANTHROPIC_API_KEY", "")
    from_env = bool(os.environ.get("ANTHROPIC_API_KEY"))
    claude_ok = False
    sdk_backend = None

    claude_installed, claude_logged_in = _check_claude_cli()

    # If the user is already logged in via `claude login`, that's sufficient —
    # the claude_agent_sdk uses the Claude CLI which reads those credentials.
    if claude_logged_in:
        print()
        success("Claude Code: logged in via Anthropic account")
        claude_ok = True
        sdk_backend = "Claude Code (Anthropic login)"
    else:
        # Try Vertex AI / Bedrock / API key auto-detection
        print()
        info("Testing Claude SDK connectivity...")
        claude_ok, sdk_backend = _test_claude_sdk(api_key if api_key else None)

        if claude_ok:
            success(f"Using {sdk_backend}")
        else:
            # No credentials found — offer login or API key
            print()
            info("No Claude credentials detected. Choose how to authenticate:")
            print()
            print(f"    {BOLD}[1]{RESET} Log in with your Anthropic account  (claude login)")
            print(f"    {BOLD}[2]{RESET} Enter an API key  (from console.anthropic.com)")
            print(f"    {BOLD}[3]{RESET} Skip  (configure Vertex AI or Bedrock credentials later)")
            print()
            choice = input("  Choice [1/2/3]: ").strip() or "1"

            if choice == "1":
                if not claude_installed:
                    print()
                    error("Claude Code CLI not found in PATH.")
                    info("Install it from: https://claude.ai/download")
                    info("Then run 'claude login' and re-run this setup.")
                else:
                    print()
                    info("Launching 'claude login' — follow the prompts in your browser...")
                    try:
                        import subprocess

                        subprocess.run(["claude", "login"], check=True)
                        _, claude_logged_in = _check_claude_cli()
                        if claude_logged_in:
                            success("Logged in successfully!")
                            claude_ok = True
                            sdk_backend = "Claude Code (Anthropic login)"
                        else:
                            warn("Login may not have completed — credentials not found.")
                            info("Run 'claude login' manually and re-run setup if needed.")
                    except subprocess.CalledProcessError:
                        error("Login failed. Run 'claude login' manually and re-run setup.")

            elif choice == "2":
                api_key = prompt_secret("Anthropic API key")
                if api_key:
                    from_env = False
                    _save_env_value("ANTHROPIC_API_KEY", api_key)
                    info("Testing with provided key...")
                    claude_ok, sdk_backend = _test_claude_sdk(api_key)

                    while not claude_ok:
                        print()
                        warn("Claude API connection failed. Debugging tips:")
                        info("  - Verify the key at console.anthropic.com")
                        info("  - Ensure no network/proxy issues")
                        print()
                        if not prompt_yes_no("Retry with this key?", default=True):
                            break
                        claude_ok, sdk_backend = _test_claude_sdk(api_key)

    # Test claude-agent-sdk
    print()
    info("Testing claude-agent-sdk...")
    if not claude_installed:
        warn("Claude Code CLI not found — agents will not be able to run")
        info("Install from: https://claude.ai/download")
        agent_sdk_ok = False
    else:
        agent_sdk_ok = _test_claude_agent_sdk()

    default_model = yaml_cfg.get("model", "claude-sonnet-4-20250514")
    model = prompt("Model", default_model)

    agents["claude"] = {
        "api_key": api_key,
        "model": model,
        "connected": claude_ok,
        "agent_sdk_ok": agent_sdk_ok,
        "from_env": from_env,
        "backend": sdk_backend,
    }

    return agents


def _test_claude_sdk(api_key: str | None = None) -> tuple[bool, str | None]:
    """Test Claude SDK connectivity, trying multiple backends.

    Tries in order: direct API (if key available), Vertex AI, Bedrock.
    Returns (success, backend_name).
    """
    try:
        import anthropic
    except ImportError:
        warn("anthropic SDK not installed — skipping connectivity test")
        return False, None

    base_test_kwargs = {
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Say ok"}],
    }

    errors: list[str] = []

    # Try direct API if key is available
    if api_key:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(model="claude-sonnet-4-20250514", **base_test_kwargs)
            if resp.content:
                success("Claude API connected successfully (direct API)")
                return True, "direct API"
        except Exception as e:
            errors.append(f"Direct API: {e}")

    # Try Vertex AI
    try:
        from anthropic import AnthropicVertex

        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
            "ANTHROPIC_VERTEX_PROJECT_ID"
        )
        region = (
            os.environ.get("GOOGLE_CLOUD_LOCATION")
            or os.environ.get("CLOUD_ML_REGION")
            or "us-east5"
        )
        if project_id:
            # Vertex AI uses @ format for model versions
            vertex_model = "claude-sonnet-4@20250514"
            info(f"Trying Vertex AI (project: {project_id}, region: {region})...")
            client = AnthropicVertex(project_id=project_id, region=region)
            resp = client.messages.create(model=vertex_model, **base_test_kwargs)
            if resp.content:
                success(f"Claude API connected successfully (Vertex AI, project: {project_id})")
                return True, f"Vertex AI ({project_id})"
        else:
            errors.append("Vertex AI: GOOGLE_CLOUD_PROJECT / ANTHROPIC_VERTEX_PROJECT_ID not set")
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"Vertex AI: {e}")

    # Try Bedrock
    try:
        from anthropic import AnthropicBedrock

        if os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"):
            info("Trying AWS Bedrock...")
            client = AnthropicBedrock()
            resp = client.messages.create(**base_test_kwargs)
            if resp.content:
                success("Claude API connected successfully (AWS Bedrock)")
                return True, "AWS Bedrock"
        else:
            errors.append("Bedrock: AWS_REGION not set")
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"Bedrock: {e}")

    # Show what was tried and why it failed
    if errors:
        for err in errors:
            info(f"  {err}")

    error("Could not connect to Claude API via any backend")
    return False, None


def _test_claude_agent_sdk() -> bool:
    """Test that the claude-agent-sdk is installed and can initialize."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions

        # Verify we can construct options (doesn't make a network call)
        # Don't specify model — let the SDK use its default, which respects
        # CLAUDE_CODE_USE_VERTEX and other env-based configuration.
        ClaudeAgentOptions(
            allowed_tools=["Read"],
        )
        success("claude-agent-sdk installed and importable")
        return True
    except ImportError:
        error("claude-agent-sdk not installed")
        info("  Install with: pip install 'claude-agent-sdk>=0.1.30'")
        return False
    except Exception as e:
        error(f"claude-agent-sdk error: {e}")
        return False


# ── Step 4: Chat Provider (LLM Backend) ──────────────────────────────────────


def _is_ollama_installed() -> bool:
    """Check if the ollama CLI is available."""
    import shutil

    return shutil.which("ollama") is not None


def _is_ollama_running(base_url: str = "http://localhost:11434") -> bool:
    """Check if Ollama is responding at the given URL."""
    import urllib.request

    try:
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _ollama_list_models(base_url: str = "http://localhost:11434") -> list[str]:
    """List locally available Ollama models."""
    import json
    import urllib.request

    try:
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _install_ollama() -> bool:
    """Install Ollama using the official install script (Linux) or Homebrew (macOS)."""
    import platform

    os_name = platform.system()

    if os_name == "Linux":
        import shutil

        # The Ollama install script requires zstd and curl
        missing_deps = []
        if not shutil.which("zstd"):
            missing_deps.append("zstd")
        if not shutil.which("curl"):
            missing_deps.append("curl")

        if missing_deps:
            dep_list = " ".join(missing_deps)
            info(f"Installing required dependencies: {dep_list}")
            if shutil.which("apt-get"):
                subprocess.run(["sudo", "apt-get", "install", "-y"] + missing_deps, check=False)
            elif shutil.which("dnf"):
                subprocess.run(["sudo", "dnf", "install", "-y"] + missing_deps, check=False)
            elif shutil.which("yum"):
                subprocess.run(["sudo", "yum", "install", "-y"] + missing_deps, check=False)
            elif shutil.which("pacman"):
                subprocess.run(["sudo", "pacman", "-S", "--noconfirm"] + missing_deps, check=False)
            else:
                error(f"Could not install {dep_list} — no supported package manager found.")
                info(f"Install {dep_list} manually and re-run setup.")
                return False

        info("Installing Ollama via official install script...")
        try:
            result = subprocess.run(
                ["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                check=False,
            )
            return result.returncode == 0
        except Exception as e:
            error(f"Install failed: {e}")
            return False

    elif os_name == "Darwin":
        import shutil

        if not shutil.which("brew"):
            error("Homebrew not found. Install Ollama manually from https://ollama.com/download")
            return False
        info("Installing Ollama via Homebrew...")
        try:
            result = subprocess.run(["brew", "install", "ollama"], check=False)
            return result.returncode == 0
        except Exception as e:
            error(f"Install failed: {e}")
            return False

    else:
        error(f"Automatic install not supported on {os_name}.")
        info("Download Ollama manually from: https://ollama.com/download")
        return False


def _start_ollama() -> bool:
    """Start the Ollama server in the background."""
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        import time

        for _ in range(10):
            time.sleep(1)
            if _is_ollama_running():
                return True
        return False
    except Exception as e:
        error(f"Failed to start Ollama: {e}")
        return False


def _pull_ollama_model(model: str) -> bool:
    """Pull an Ollama model (streams progress to terminal)."""
    try:
        result = subprocess.run(["ollama", "pull", model], check=False)
        return result.returncode == 0
    except Exception as e:
        error(f"Pull failed: {e}")
        return False


def step_chat_provider(existing: dict) -> dict:
    step_header(4, "Chat Provider (LLM Backend)")

    yaml_cfg = existing.get("_yaml", {})
    existing_cp = yaml_cfg.get("chat_provider", {})
    existing_provider = existing_cp.get("provider", "anthropic")

    info("The chat provider controls which LLM powers the Discord chat interface.")
    info("This is separate from the Claude Code agents that execute tasks.")
    print()
    print(f"    {BOLD}[1] Anthropic{RESET}  (Claude API — default, same as task agents)")
    print(f"    {BOLD}[2] Ollama{RESET}     (local models — free, private, no API key needed)")
    print()

    default_choice = "2" if existing_provider == "ollama" else "1"
    choice = prompt("Choice", default_choice)

    if choice != "2":
        model = existing_cp.get("model", "")
        if model:
            info(f"Using configured model: {model}")
        else:
            info("Using default Anthropic model (same as task agents)")
        return {"provider": "anthropic", "model": model, "base_url": ""}

    # ── Ollama setup ──

    default_base_url = existing_cp.get("base_url", "http://localhost:11434/v1")
    check_url = default_base_url.rstrip("/")
    if check_url.endswith("/v1"):
        check_url = check_url[:-3]

    if not _is_ollama_installed():
        warn("Ollama is not installed.")
        if prompt_yes_no("Install Ollama now?", default=True):
            if _install_ollama():
                success("Ollama installed successfully")
            else:
                error("Ollama installation failed")
                info("Install manually from: https://ollama.com/download")
                info("Then re-run this setup.")
                return {"provider": "anthropic", "model": "", "base_url": ""}
        else:
            info("Skipping Ollama setup — falling back to Anthropic.")
            return {"provider": "anthropic", "model": "", "base_url": ""}

    print()
    if not _is_ollama_running(check_url):
        warn("Ollama is installed but not running.")
        if prompt_yes_no("Start Ollama now?", default=True):
            info("Starting Ollama server...")
            if _start_ollama():
                success("Ollama is running")
            else:
                error("Could not start Ollama")
                info("Start it manually with: ollama serve")
        else:
            info("Continuing without verifying Ollama connectivity.")
    else:
        success("Ollama is running")

    print()
    local_models = _ollama_list_models(check_url)
    if local_models:
        info("Locally available models:")
        for i, m in enumerate(local_models, 1):
            print(f"      {i}. {m}")
    else:
        info("No models downloaded yet.")

    print()
    default_model = existing_cp.get("model", "")
    if not default_model:
        default_model = local_models[0] if local_models else "qwen3.5:35b"

    model = prompt("Model name or number", default_model)

    # Resolve numeric selection to model name
    if model.isdigit() and local_models and 1 <= int(model) <= len(local_models):
        model = local_models[int(model) - 1]

    if model not in local_models:
        print()
        if prompt_yes_no(f"Model '{model}' is not downloaded. Pull it now?", default=True):
            info(f"Pulling {model} (this may take a while)...")
            if _pull_ollama_model(model):
                success(f"Model '{model}' is ready")
            else:
                error(f"Failed to pull '{model}'")
                info("Pull it manually with: ollama pull " + model)
        else:
            info(f"Pull it later with: ollama pull {model}")
    else:
        success(f"Model '{model}' is available locally")

    base_url = prompt("Ollama base URL", default_base_url)

    print()
    try:
        import openai  # noqa: F401

        success("openai package is installed (required for Ollama provider)")
    except ImportError:
        warn("openai Python package is not installed (required for Ollama provider)")
        if prompt_yes_no("Install it now?", default=True):
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "openai>=1.0.0", "--quiet"],
                check=False,
            )
            if result.returncode == 0:
                success("openai package installed")
            else:
                error("Failed to install openai package")
                info("Install manually with: pip install 'openai>=1.0.0'")
        else:
            info("Install it before running with Ollama: pip install 'openai>=1.0.0'")

    return {"provider": "ollama", "model": model, "base_url": base_url}


# ── Step 5: Scheduling & Budget ──────────────────────────────────────────────


def step_scheduling(existing: dict) -> dict:
    step_header(5, "Scheduling & Budget")

    yaml_cfg = existing.get("_yaml", {})
    existing_sched = yaml_cfg.get("scheduling", {})
    existing_pr = yaml_cfg.get("pause_retry", {})

    config: dict = {
        "global_token_budget_daily": yaml_cfg.get("global_token_budget_daily"),
        "scheduling": {
            "rolling_window_hours": int(existing_sched.get("rolling_window_hours", 24)),
            "min_task_guarantee": True,
        },
        "pause_retry": {
            "rate_limit_backoff_seconds": int(existing_pr.get("rate_limit_backoff_seconds", 60)),
            "token_exhaustion_retry_seconds": int(
                existing_pr.get("token_exhaustion_retry_seconds", 300)
            ),
        },
    }

    if prompt_yes_no(
        "Set a daily token budget?", default=bool(config["global_token_budget_daily"])
    ):
        default_budget = str(config["global_token_budget_daily"] or "")
        budget = prompt("Daily token budget (e.g. 1000000)", default_budget)
        if budget.isdigit():
            config["global_token_budget_daily"] = int(budget)

    if prompt_yes_no("Customize scheduling/retry defaults?", default=False):
        val = prompt("Rolling window hours", str(config["scheduling"]["rolling_window_hours"]))
        config["scheduling"]["rolling_window_hours"] = int(val) if val.isdigit() else 24

        val = prompt(
            "Rate-limit backoff seconds",
            str(config["pause_retry"]["rate_limit_backoff_seconds"]),
        )
        config["pause_retry"]["rate_limit_backoff_seconds"] = int(val) if val.isdigit() else 60

        val = prompt(
            "Token-exhaustion retry seconds",
            str(config["pause_retry"]["token_exhaustion_retry_seconds"]),
        )
        config["pause_retry"]["token_exhaustion_retry_seconds"] = int(val) if val.isdigit() else 300
    else:
        info(
            f"Using defaults: {config['scheduling']['rolling_window_hours']}h window, "
            f"{config['pause_retry']['rate_limit_backoff_seconds']}s rate-limit backoff, "
            f"{config['pause_retry']['token_exhaustion_retry_seconds']}s token retry"
        )

    return config


# ── Step 6: Write Config ─────────────────────────────────────────────────────


def step_write_config(
    workspace: str,
    db_config: dict,
    discord_cfg: dict,
    agents_cfg: dict,
    sched_cfg: dict,
    chat_provider_cfg: dict,
) -> Path:
    step_header(6, "Write Configuration")

    config_dir = Path(os.path.expanduser("~/.agent-queue"))
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    env_path = config_dir / ".env"

    # Build .env
    env_lines = [
        f"DISCORD_BOT_TOKEN={discord_cfg['bot_token']}",
    ]
    claude_cfg = agents_cfg.get("claude")
    if claude_cfg and not claude_cfg["from_env"]:
        env_lines.append(f"ANTHROPIC_API_KEY={claude_cfg['api_key']}")

    env_path.write_text("\n".join(env_lines) + "\n")
    os.chmod(env_path, 0o600)
    success(f"Secrets written to {env_path} (mode 600)")

    # Build YAML
    channels = discord_cfg["channels"]
    yaml_lines = [
        f"workspace_dir: {workspace}",
    ]

    if db_config.get("backend") == "postgresql":
        yaml_lines += [
            "",
            "database:",
            f"  url: {db_config['url']}",
            f"  pool_min_size: {db_config.get('pool_min_size', 2)}",
            f"  pool_max_size: {db_config.get('pool_max_size', 10)}",
        ]
    else:
        yaml_lines.append(f"database_path: {db_config['url']}")

    yaml_lines += [
        "",
        "discord:",
        "  bot_token: ${DISCORD_BOT_TOKEN}",
        f'  guild_id: "{discord_cfg["guild_id"]}"',
        "  channels:",
        f"    control: {channels['control']}",
        f"    notifications: {channels['notifications']}",
        f"    agent_questions: {channels['agent_questions']}",
    ]

    if discord_cfg["authorized_users"]:
        yaml_lines.append("  authorized_users:")
        for uid in discord_cfg["authorized_users"]:
            yaml_lines.append(f'    - "{uid}"')

    # Per-project channel configuration
    ppc = discord_cfg.get("per_project_channels", {})
    if ppc.get("auto_create"):
        yaml_lines.append("  per_project_channels:")
        yaml_lines.append("    auto_create: true")
        yaml_lines.append(f'    naming_convention: "{ppc["naming_convention"]}"')

        if ppc.get("category_name"):
            yaml_lines.append(f'    category_name: "{ppc["category_name"]}"')

        private = ppc.get("private", True)
        yaml_lines.append(f"    private: {'true' if private else 'false'}")

    yaml_lines.append("")

    # Chat provider config
    cp = chat_provider_cfg
    if cp.get("provider") and cp["provider"] != "anthropic":
        yaml_lines.append("chat_provider:")
        yaml_lines.append(f"  provider: {cp['provider']}")
        if cp.get("model"):
            yaml_lines.append(f"  model: {cp['model']}")
        if cp.get("base_url"):
            yaml_lines.append(f"  base_url: {cp['base_url']}")
        yaml_lines.append("")
    elif cp.get("model"):
        yaml_lines.append("chat_provider:")
        yaml_lines.append("  provider: anthropic")
        yaml_lines.append(f"  model: {cp['model']}")
        yaml_lines.append("")

    if sched_cfg.get("global_token_budget_daily"):
        yaml_lines.append(f"global_token_budget_daily: {sched_cfg['global_token_budget_daily']}")
        yaml_lines.append("")

    sched = sched_cfg["scheduling"]
    yaml_lines += [
        "scheduling:",
        f"  rolling_window_hours: {sched['rolling_window_hours']}",
        "  min_task_guarantee: true",
        "",
    ]

    pr = sched_cfg["pause_retry"]
    yaml_lines += [
        "pause_retry:",
        f"  rate_limit_backoff_seconds: {pr['rate_limit_backoff_seconds']}",
        f"  token_exhaustion_retry_seconds: {pr['token_exhaustion_retry_seconds']}",
    ]

    config_path.write_text("\n".join(yaml_lines) + "\n")
    success(f"Config written to {config_path}")

    # Offer to add .env sourcing to shell profile
    if env_lines:
        _offer_shell_env(env_path)

    return config_path


def _offer_shell_env(env_path: Path):
    """Offer to add .env sourcing to the user's shell profile."""
    source_line = f'set -a; source "{env_path}"; set +a'

    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        profile = Path.home() / ".zshrc"
    else:
        profile = Path.home() / ".bashrc"

    # Check if already present
    if profile.exists() and source_line in profile.read_text():
        info(f"Shell profile already sources {env_path}")
        return

    print()
    if prompt_yes_no(f"Add env sourcing to {profile.name}?", default=True):
        with open(profile, "a") as f:
            f.write(f"\n# agent-queue secrets\n{source_line}\n")
        success(f"Added to {profile}")
        warn(f"Run: source {profile}  (or open a new terminal)")
    else:
        info(f"You'll need to source {env_path} before running agent-queue")


# ── Step 7: Test Connectivity ─────────────────────────────────────────────────


def step_test_connectivity(discord_cfg: dict, agents_cfg: dict, chat_provider_cfg: dict):
    step_header(7, "Connectivity Summary")

    if discord_cfg.get("connected"):
        success("Discord: connected")
    else:
        error("Discord: not verified")

    claude_cfg = agents_cfg.get("claude")
    if claude_cfg and claude_cfg.get("connected"):
        backend = claude_cfg.get("backend", "unknown")
        success(f"Claude API: connected ({backend})")
    elif claude_cfg:
        error("Claude API: not verified")

    if claude_cfg and claude_cfg.get("agent_sdk_ok"):
        success("claude-agent-sdk: installed")
    elif claude_cfg:
        error("claude-agent-sdk: not installed")

    cp = chat_provider_cfg
    if cp.get("provider") == "ollama":
        check_url = cp.get("base_url", "http://localhost:11434/v1").rstrip("/")
        if check_url.endswith("/v1"):
            check_url = check_url[:-3]
        if _is_ollama_running(check_url):
            success(f"Chat provider: Ollama ({cp.get('model', 'default')})")
        else:
            warn("Chat provider: Ollama (not running — start with: ollama serve)")
    else:
        success("Chat provider: Anthropic (default)")


# ── Step 8: Launch Daemon ─────────────────────────────────────────────────────


def step_launch(config_path: Path):
    step_header(8, "Launch Daemon")

    log_dir = Path(os.path.expanduser("~/.agent-queue"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "daemon.log"

    cmd = f"agent-queue {config_path}"

    if prompt_yes_no("Start the daemon now?", default=False):
        print()
        info(f"Starting: {cmd}")
        info(f"Log file: {log_path}")
        log_file = open(log_path, "a")
        proc = subprocess.Popen(
            ["agent-queue", str(config_path)],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        success(f"Daemon started (PID {proc.pid})")
        info(f"Stop with: kill {proc.pid}")
        info(f"View logs: tail -f {log_path}")
    else:
        print()
        info("To start later, run:")
        print(f"    {BOLD}{cmd}{RESET}")
        info(f"Logs will be written to: {log_path}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    banner()

    # Load existing config for pre-filling defaults
    existing = _load_existing_config()
    if existing.get("_yaml"):
        info("Found existing configuration — values will be pre-filled as defaults")
        print()

    workspace = step_directories(existing)
    db_config = step_database(existing)
    discord_cfg = step_discord(existing)
    agents_cfg = step_agents(existing)
    chat_provider_cfg = step_chat_provider(existing)
    sched_cfg = step_scheduling(existing)

    config_path = step_write_config(
        workspace, db_config, discord_cfg, agents_cfg, sched_cfg, chat_provider_cfg
    )

    # Run data migration if switching from SQLite to PostgreSQL
    migrate_from = db_config.get("_migrate_from")
    if migrate_from:
        info("Migrating data from SQLite to PostgreSQL...")
        try:
            from src.database.migrate_sqlite_to_pg import migrate_sqlite_to_postgres

            def _progress(table: str, count: int):
                if count:
                    success(f"  {table}: {count} rows")
                else:
                    info(f"  {table}: empty")

            counts = asyncio.run(
                migrate_sqlite_to_postgres(migrate_from, db_config["url"], progress_cb=_progress)
            )
            total = sum(counts.values())
            success(f"Migration complete: {total} total rows across {len(counts)} tables")
        except Exception as e:
            error(f"Migration failed: {e}")
            warn("Your SQLite database is unchanged. You can retry migration later.")

    step_test_connectivity(discord_cfg, agents_cfg, chat_provider_cfg)
    step_launch(config_path)

    print(f"\n{GREEN}{BOLD}Setup complete!{RESET}\n")


if __name__ == "__main__":
    main()
