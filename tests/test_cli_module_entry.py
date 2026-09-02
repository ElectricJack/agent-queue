"""``python -m src.cli.app`` must expose the same commands as the ``aq`` script.

Running the CLI as a module imports ``src/cli/app.py`` twice (as ``__main__``
and as ``src.cli.app``); the hand-crafted commands register on the latter.
Without the delegation in ``app.py``'s ``__main__`` block they vanish from
``python -m src.cli.app --help`` — which is how a pool worker's bootstrap
prompt ended up telling an agent to run an ``aq inbox`` that "did not exist".
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("command", ["inbox", "reply", "message", "schema", "prime", "handoff"])
def test_module_entry_exposes_hand_crafted_commands(command):
    proc = subprocess.run(
        [sys.executable, "-m", "src.cli.app", command, "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "No such command" not in proc.stderr


# ---------------------------------------------------------------------------
# Duplicate registration on a shared click group — the root cause behind
# tests/test_cli_logs.py's cross-file failures (fixed in dceb2c3f).
#
# Every ``src/cli/*.py`` module decorates commands onto the *same* click
# group objects, and each module reaches them through the circular
# ``from .app import cli``. Registering one name twice therefore makes the
# winner depend on which module a process imported first: ``app.py``'s
# import order decides it normally, but any process that imports the losing
# module directly (a test file, a script) finishes that module's body last
# and silently replaces the real command with the duplicate. That is how CI
# lost ``aq logs -F``. Catch the duplicate at the source instead of waiting
# for a downstream test to fail in only some orderings.
# ---------------------------------------------------------------------------


def _decorated_command_registrations() -> dict[tuple[str, str, str], list[str]]:
    """Map ``(group variable, "command"|"group", name)`` -> ``file:line`` sites."""
    import ast
    import collections
    import pathlib

    sites: dict[tuple[str, str, str], list[str]] = collections.defaultdict(list)
    for path in sorted(pathlib.Path(REPO, "src", "cli").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                func = call.func if call is not None else decorator
                if not isinstance(func, ast.Attribute) or func.attr not in ("command", "group"):
                    continue
                if not isinstance(func.value, ast.Name):
                    continue  # e.g. ``@some.attr.command`` — not a plain group variable
                # Click's default name is the function name, underscores dashed.
                name = node.name.lower().replace("_", "-")
                if call is not None:
                    if call.args and isinstance(call.args[0], ast.Constant):
                        name = call.args[0].value
                    else:
                        for keyword in call.keywords:
                            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                                name = keyword.value.value
                rel = os.path.relpath(path, REPO)
                sites[(func.value.id, func.attr, name)].append(f"{rel}:{node.lineno}")
    return sites


def test_no_command_name_is_registered_twice_on_the_same_group():
    duplicates = {
        key: where for key, where in _decorated_command_registrations().items() if len(where) > 1
    }
    assert not duplicates, (
        "these command names are decorated onto the same click group more than once, so "
        "which one wins depends on module import order:\n"
        + "\n".join(
            f"  @{group}.{kind}({name!r}): {', '.join(where)}"
            for (group, kind, name), where in duplicates.items()
        )
    )


def test_inbox_command_survives_direct_agent_surface_import():
    """``aq inbox`` must resolve to ``src.cli.messages`` in any import order.

    ``src/cli/agent_surface.py`` used to carry a superseded no-op ``inbox``
    stub next to the real one in ``messages.py``. ``app.py`` imports
    ``agent_surface`` first so the real command normally won, but importing
    ``src.cli.agent_surface`` directly flipped it and downgraded ``aq inbox``
    to a command that silently did nothing.
    """
    code = (
        "import src.cli.agent_surface\n"
        "from src.cli.app import cli\n"
        "cmd = cli.commands['inbox']\n"
        "assert cmd.callback.__module__ == 'src.cli.messages', cmd.callback.__module__\n"
        "opts = {o.name for o in cmd.params}\n"
        "assert {'to', 'to_kind', 'to_id', 'inject'} <= opts, opts\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
