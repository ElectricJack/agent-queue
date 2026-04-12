"""Interactive menu components using prompt_toolkit.

Provides fuzzy-searchable selection menus, multi-step wizards,
and confirmation prompts for the CLI.
"""

from __future__ import annotations

from typing import Any, Sequence

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.completion import FuzzyWordCompleter, WordCompleter
from prompt_toolkit.shortcuts import radiolist_dialog, yes_no_dialog
from prompt_toolkit.validation import Validator

from rich.console import Console

from .styles import STATUS_ICONS, TASK_TYPE_ICONS


console = Console()


def select_from_list(
    items: Sequence[tuple[str, str]],
    title: str = "Select an item",
    allow_cancel: bool = True,
) -> str | None:
    """Show an interactive radio-button selection dialog.

    Parameters
    ----------
    items:
        List of (value, display_label) tuples.
    title:
        Dialog title text.
    allow_cancel:
        If True, user can press Escape to cancel.

    Returns
    -------
    The selected value string, or None if cancelled.
    """
    if not items:
        console.print("[dim]No items to select from.[/]")
        return None

    result = radiolist_dialog(
        title=title,
        text="Use arrow keys to navigate, Enter to select:",
        values=items,
    ).run()

    return result


def fuzzy_select_task(
    tasks: list[Any],
    prompt_text: str = "Search tasks: ",
) -> Any | None:
    """Fuzzy-search task selection.

    Displays task IDs and titles as completions, returns the selected Task.
    """
    if not tasks:
        console.print("[dim]No tasks available.[/]")
        return None

    # Build completion entries: "id - title"
    task_map: dict[str, Any] = {}
    words: list[str] = []
    for t in tasks:
        key = f"{t.id}"
        task_map[key] = t
        words.append(key)

    # Show numbered list for reference
    console.print()
    for i, t in enumerate(tasks[:30], 1):
        icon = STATUS_ICONS.get(t.status.value, "⚪")
        console.print(f"  {icon} [bold bright_cyan]{t.id}[/] {t.title[:60]}")
    if len(tasks) > 30:
        console.print(f"  [dim]... and {len(tasks) - 30} more[/]")
    console.print()

    completer = FuzzyWordCompleter(words)

    try:
        selected = pt_prompt(
            prompt_text,
            completer=completer,
            complete_while_typing=True,
        ).strip()
    except (KeyboardInterrupt, EOFError):
        return None

    # Try exact match first, then prefix match
    if selected in task_map:
        return task_map[selected]

    # Fuzzy fallback: find first task whose ID starts with input
    for key, task in task_map.items():
        if key.startswith(selected):
            return task

    return None


def confirm(message: str, default: bool = False) -> bool:
    """Simple yes/no confirmation prompt."""
    try:
        result = yes_no_dialog(
            title="Confirm",
            text=message,
        ).run()
        return result if result is not None else default
    except (KeyboardInterrupt, EOFError):
        return default


def prompt_input(
    label: str,
    default: str = "",
    required: bool = True,
    completer_words: list[str] | None = None,
    multiline: bool = False,
) -> str | None:
    """Prompt for text input with optional completion and validation.

    Returns
    -------
    The input string, or None if cancelled.
    """
    completer = None
    if completer_words:
        completer = FuzzyWordCompleter(completer_words)

    validator = None
    if required:
        validator = Validator.from_callable(
            lambda text: len(text.strip()) > 0,
            error_message="This field is required.",
        )

    try:
        result = pt_prompt(
            f"{label}: ",
            default=default,
            completer=completer,
            complete_while_typing=bool(completer_words),
            validator=validator,
            multiline=multiline,
        )
        return result.strip() if result else None
    except (KeyboardInterrupt, EOFError):
        return None


def prompt_choice(
    label: str,
    choices: list[str],
    default: str | None = None,
) -> str | None:
    """Prompt to select from a fixed set of choices."""
    completer = WordCompleter(choices, sentence=True)

    validator = Validator.from_callable(
        lambda text: text.strip() in choices or text.strip() == "",
        error_message=f"Must be one of: {', '.join(choices)}",
    )

    try:
        result = pt_prompt(
            f"{label} [{'/'.join(choices)}]: ",
            default=default or "",
            completer=completer,
            validator=validator,
        )
        return result.strip() if result else default
    except (KeyboardInterrupt, EOFError):
        return default


def task_creation_wizard(
    project_ids: list[str],
) -> dict[str, Any] | None:
    """Interactive multi-step task creation wizard.

    Returns
    -------
    Dict with task creation parameters, or None if cancelled.
    """
    console.print()
    console.print("[bold bright_white]📝 Create New Task[/]")
    console.print("[dim]Press Ctrl+C at any step to cancel.[/]")
    console.print()

    # Step 1: Project
    console.print("[bold cyan]Step 1/6:[/] Select project")
    project_id = prompt_input(
        "Project ID",
        completer_words=project_ids,
        required=True,
    )
    if not project_id:
        return None

    # Step 2: Title
    console.print()
    console.print("[bold cyan]Step 2/6:[/] Task title")
    title = prompt_input("Title", required=True)
    if not title:
        return None

    # Step 3: Description
    console.print()
    console.print("[bold cyan]Step 3/6:[/] Task description")
    console.print("[dim]  (Enter a single line, or use Ctrl+D for multiline)[/]")
    description = prompt_input("Description", required=True)
    if not description:
        return None

    # Step 4: Priority
    console.print()
    console.print("[bold cyan]Step 4/6:[/] Priority (1-300, default 100)")
    pri_str = prompt_input("Priority", default="100", required=False)
    try:
        priority = int(pri_str) if pri_str else 100
    except ValueError:
        priority = 100

    # Step 5: Task type
    console.print()
    console.print("[bold cyan]Step 5/6:[/] Task type")
    task_types = ["feature", "bugfix", "refactor", "test", "docs", "chore", "research", "plan"]
    type_display = ", ".join(f"{TASK_TYPE_ICONS.get(t, '')} {t}" for t in task_types)
    console.print(f"  [dim]{type_display}[/]")
    task_type = prompt_choice("Type", task_types, default="feature")

    # Step 6: Approval required
    console.print()
    console.print("[bold cyan]Step 6/6:[/] Require approval before execution?")
    approval = prompt_choice("Require approval", ["yes", "no"], default="no")
    requires_approval = approval == "yes"

    console.print()
    console.print("[bold green]✅ Task configuration complete![/]")

    return {
        "project_id": project_id,
        "title": title,
        "description": description,
        "priority": priority,
        "task_type": task_type,
        "requires_approval": requires_approval,
    }
