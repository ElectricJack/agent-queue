"""Vault management CLI commands (``aq vault ...``).

Provides the ``aq vault migrate`` command for running consolidated vault
migrations — either as a dry-run preview or live execution.  All migration
logic lives in ``src/vault.py``; this module just provides the CLI surface
and Rich-formatted output.
"""

from __future__ import annotations

import os

import click
from rich.panel import Panel
from rich.table import Table

from .app import cli, console


def _resolve_data_dir(data_dir: str | None) -> str:
    """Resolve the data directory, defaulting to ``~/.agent-queue``."""
    if data_dir:
        return os.path.expanduser(data_dir)

    # Try loading from config if available
    try:
        from src.config import load_config

        config = load_config(os.path.expanduser("~/.agent-queue/config.yaml"))
        return config.data_dir
    except Exception:
        return os.path.expanduser("~/.agent-queue")


@cli.group()
def vault() -> None:
    """Vault management — migrate, inspect, and organize the knowledge base."""


@vault.command("migrate")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would happen without making changes.",
)
@click.option(
    "--data-dir",
    default=None,
    help="Data directory (default: from config or ~/.agent-queue).",
)
@click.option(
    "--project",
    "project_ids",
    multiple=True,
    help="Specific project ID(s) to migrate. Can be repeated. "
    "Default: auto-discover from filesystem.",
)
def vault_migrate(
    dry_run: bool,
    data_dir: str | None,
    project_ids: tuple[str, ...],
) -> None:
    """Run all vault migrations (idempotent, safe to run multiple times).

    Consolidates all Phase 1 vault migrations into a single operation:

    \b
    1. Obsidian config (.obsidian/ → vault/.obsidian/)
    2. Vault directory layout creation
    3. Notes migration (notes/{project}/ → vault/projects/{project}/notes/)
    4. Memory file copy (memory/{project}/ → vault/projects/{project}/memory/)
    5. Rule file migration (memory/*/rules/ → vault playbooks)

    Use --dry-run to preview what would happen without making changes.
    """
    from src.vault import run_vault_migration

    resolved_dir = _resolve_data_dir(data_dir)

    if not os.path.isdir(resolved_dir):
        console.print(f"[bold red]Error:[/] Data directory does not exist: {resolved_dir}")
        raise SystemExit(1)

    # Convert tuple to list or None for auto-discovery
    pids = list(project_ids) if project_ids else None

    mode_label = "[bold yellow]DRY RUN[/]" if dry_run else "[bold green]LIVE[/]"
    console.print(f"\n🗄️  Vault Migration ({mode_label})")
    console.print(f"   Data dir: [dim]{resolved_dir}[/dim]\n")

    report = run_vault_migration(
        data_dir=resolved_dir,
        project_ids=pids,
        dry_run=dry_run,
    )

    # --- Projects discovered ---
    projects = report["projects_discovered"]
    if projects:
        console.print(f"   Projects: {', '.join(projects)}")
    else:
        console.print("   Projects: [dim](none discovered)[/dim]")
    console.print()

    # --- Results table ---
    table = Table(title="Migration Results", show_lines=True, expand=False)
    table.add_column("Step", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Details")

    # Obsidian
    obs = report.get("obsidian", {})
    obs_action = obs.get("action", "skip")
    if "move" in obs_action:
        table.add_row(
            ".obsidian config",
            "[green]✓ moved[/]" if not dry_run else "[yellow]→ would move[/]",
            "memory/.obsidian/ → vault/.obsidian/",
        )
    else:
        reason = obs.get("reason", "already migrated")
        table.add_row(".obsidian config", "[dim]skipped[/]", f"[dim]{reason}[/dim]")

    # Notes (per-project)
    notes = report.get("notes", {})
    for pid, info in notes.items():
        if dry_run:
            wm = info.get("would_move", 0)
            ws = info.get("would_skip", 0)
            if wm or ws:
                table.add_row(
                    f"notes/{pid}",
                    "[yellow]→ pending[/]",
                    f"{wm} to move, {ws} already there",
                )
            else:
                table.add_row(f"notes/{pid}", "[dim]skipped[/]", "[dim]nothing to do[/dim]")
        else:
            if info.get("moved"):
                table.add_row(f"notes/{pid}", "[green]✓ migrated[/]", "files moved to vault")
            else:
                table.add_row(
                    f"notes/{pid}", "[dim]skipped[/]", "[dim]no source or already done[/dim]"
                )

    # Memory (per-project)
    memory = report.get("memory", {})
    for pid, info in memory.items():
        if dry_run:
            wc = info.get("would_copy", 0)
            wu = info.get("would_update", 0)
            ws = info.get("would_skip", 0)
            total = wc + wu + ws
            if total:
                parts = []
                if wc:
                    parts.append(f"{wc} to copy")
                if wu:
                    parts.append(f"{wu} to update")
                if ws:
                    parts.append(f"{ws} up to date")
                table.add_row(
                    f"memory/{pid}",
                    "[yellow]→ pending[/]",
                    ", ".join(parts),
                )
            else:
                table.add_row(f"memory/{pid}", "[dim]skipped[/]", "[dim]nothing to do[/dim]")
        else:
            if info.get("copied"):
                table.add_row(f"memory/{pid}", "[green]✓ copied[/]", "files synced to vault")
            else:
                table.add_row(
                    f"memory/{pid}",
                    "[dim]skipped[/]",
                    "[dim]no source or up to date[/dim]",
                )

    # Rules
    rules = report.get("rules", {})
    if dry_run:
        rm = rules.get("would_move", 0)
        rs = rules.get("would_skip", 0)
        if rm or rs:
            table.add_row(
                "rule files",
                "[yellow]→ pending[/]",
                f"{rm} to move, {rs} already there",
            )
        else:
            table.add_row("rule files", "[dim]skipped[/]", "[dim]nothing to migrate[/dim]")
    else:
        rm = rules.get("moved", 0)
        rs = rules.get("skipped", 0)
        re = rules.get("errors", 0)
        parts = []
        if rm:
            parts.append(f"{rm} moved")
        if rs:
            parts.append(f"{rs} skipped")
        if re:
            parts.append(f"[red]{re} errors[/red]")
        table.add_row(
            "rule files",
            "[green]✓ done[/]" if not re else "[red]⚠ errors[/]",
            ", ".join(parts) or "[dim]nothing to migrate[/dim]",
        )

    console.print(table)
    console.print()

    # --- Summary panel ---
    s = report["summary"]
    if dry_run:
        summary_text = (
            f"[bold yellow]Preview complete[/] — no changes made.\n"
            f"Would move: {s['total_moved']}  ·  "
            f"Would copy: {s['total_copied']}  ·  "
            f"Would skip: {s['total_skipped']}"
        )
    else:
        summary_text = (
            f"[bold green]Migration complete.[/]\n"
            f"Moved: {s['total_moved']}  ·  "
            f"Copied: {s['total_copied']}  ·  "
            f"Skipped: {s['total_skipped']}  ·  "
            f"Errors: {s['total_errors']}"
        )

    console.print(Panel(summary_text, title="Summary", border_style="blue"))

    if s["total_errors"] > 0:
        raise SystemExit(1)
