"""Fresh, context-scoped vault templates for daemon-generated worker instructions."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

SHIPPED_TASK_TEMPLATES = Path(__file__).parent / "prompts" / "task_templates"
_template_dir: ContextVar[Path | None] = ContextVar("task_template_dir", default=None)
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*\}\}")


def bind_task_templates(data_dir: str | Path) -> None:
    """Bind the configured vault in the daemon's async startup context."""
    _template_dir.set(Path(data_dir) / "vault" / "templates" / "tasks")


@contextmanager
def task_template_scope(data_dir: str | Path) -> Iterator[None]:
    """Temporarily bind a vault for an embedded caller or isolated test."""
    token = _template_dir.set(Path(data_dir) / "vault" / "templates" / "tasks")
    try:
        yield
    finally:
        _template_dir.reset(token)


def render_task_template(name: str, **facts: object) -> str:
    """Read an installed template or its shipped default and substitute facts once.

    Installed edits are authoritative: read/encoding/placeholder errors propagate.
    No renderer writes the vault, and no template content executes as code.
    """
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError(f"Invalid task template name: {name!r}")
    shipped = SHIPPED_TASK_TEMPLATES / f"{name}.md"
    if not shipped.is_file():
        raise ValueError(f"Unknown task template: {name}")
    directory = _template_dir.get()
    path = directory / shipped.name if directory is not None else shipped
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        body = shipped.read_text(encoding="utf-8")

    remaining = _PLACEHOLDER.sub("", body)
    if "{{" in remaining or "}}" in remaining:
        raise ValueError(f"Task template {name!r} has malformed placeholders")
    missing = set(_PLACEHOLDER.findall(body)) - facts.keys()
    if missing:
        raise ValueError(
            f"Task template {name!r} has unknown variables: {', '.join(sorted(missing))}"
        )
    return _PLACEHOLDER.sub(lambda match: str(facts[match.group(1)]), body)


def editable_task_template_path(path: str, data_dir: str) -> str | None:
    """Allow operator/supervisor file tools to edit only the task-template directory.

    Command capability checks still apply. Resolve symlinks before checking the
    directory boundary; a template symlink cannot grant access to another path.
    """
    from src.commands.principal import PrincipalKind, current_principal

    principal = current_principal()
    if principal is not None and not (
        principal.kind is PrincipalKind.LOCAL
        or (principal.kind is PrincipalKind.SESSION and principal.elevated)
    ):
        return None
    root = Path(data_dir).resolve() / "vault" / "templates" / "tasks"
    if root.resolve() != root:
        return None
    resolved = Path(path).resolve()
    if resolved == root or resolved.is_relative_to(root):
        return str(resolved)
    return None
