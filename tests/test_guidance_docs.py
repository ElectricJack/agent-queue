"""Every ``aq …`` the static guidance tells an agent to run must parse against the CLI.

``src/skills/*/SKILL.md`` and the prime templates are static guidance loaded into
a worker's context, so a wrong argument shape there costs a turn to discover and
is invisible to every test that exercises the CLI itself.  Three ways it has rotted in practice, all
found in one audit:

* a subcommand that no longer exists (``aq task tree``, ``aq task restore``,
  ``aq gate list``) — the code moved and the doc did not;
* a positional id passed to an auto-generated command, which takes ``--task-id``
  and nothing else (``aq task children <id>``);
* a group-level flag written after the subcommand (``aq task list --json``).

This module resolves each documented invocation against the live Click tree and
fails on any of the three.  It is the ``TestStaticGuidanceStaysOnTheAgentSurface``
idea in ``test_prime_renderer.py`` — which asks whether a prime template names a
command the token *refuses* — extended to argument *shape*, and widened from the
prime templates to the skills, which had never been scanned at all.
"""

from __future__ import annotations

import re
import shlex
import textwrap
from pathlib import Path

import click
import pytest

SKILLS_DIR = Path(__file__).resolve().parents[1] / "src" / "skills"
PRIME_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "prime" / "templates"

_FENCE = re.compile(r"^```(\w*)\s*$")
_INLINE = re.compile(r"`([^`]+)`")
#: Fenced blocks whose contents are shell the reader is meant to run.
_SHELL_LANGS = {"bash", "sh", "shell", "console"}
#: Shell metacharacters that end the ``aq`` invocation and start something else.
_STOP = {"#", "|", "||", "&&", ";", ">", ">>", "2>&1", "—"}


def _invocations(text: str) -> list[tuple[int, str]]:
    """``(line number, command)`` for every ``aq …`` the doc presents as runnable.

    Reads shell fenced blocks (joining ``\\`` continuations) and inline code
    spans in prose, which is where a command shape is just as load-bearing.
    Prose *outside* backticks is left alone: it names commands without
    committing to an argument list.
    """
    out: list[tuple[int, str]] = []
    lang: str | None = None
    pending: tuple[int, str] | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        fence = _FENCE.match(line)
        if fence:
            lang = fence.group(1) if lang is None else None
            continue
        if lang in _SHELL_LANGS:
            if pending is not None:
                pending = (pending[0], f"{pending[1]} {line.rstrip(chr(92)).strip()}")
            elif line.startswith("aq ") or line == "aq":
                pending = (lineno, line.rstrip(chr(92)).strip())
            else:
                continue
            if line.endswith(chr(92)):  # backslash continuation
                continue
            out.append(pending)
            pending = None
        elif lang is None:
            for span in _INLINE.finditer(raw):
                fragment = " ".join(span.group(1).split())
                if fragment.startswith("aq "):
                    out.append((lineno, fragment))
    return out


def _tokens(command: str) -> list[str]:
    """Shell-split ``command``, stopping at the first pipe/comment/redirect.

    ``[--recursive]`` and friends are doc notation for "optional"; the brackets
    are stripped so the option inside is still checked.
    """
    try:
        raw = shlex.split(command, comments=False)
    except ValueError:  # an unbalanced quote in a doc snippet
        raw = command.split()
    kept: list[str] = []
    for token in raw:
        if token in _STOP or token.startswith("#"):
            break
        kept.append(token.strip("[]"))
    return [token for token in kept if token]


def _resolve(tokens: list[str]) -> tuple[click.Command, int]:
    """The deepest real command ``tokens`` names, and how many tokens it ate."""
    from src.cli.app import cli

    node: click.Command = cli
    depth = 1  # "aq"
    for token in tokens[1:]:
        if isinstance(node, click.Group) and token in node.commands:
            node, depth = node.commands[token], depth + 1
            continue
        break
    return node, depth


def _problems(source: str, lineno: int, command: str) -> list[str]:
    tokens = _tokens(command)
    if not tokens or tokens[0] != "aq":
        return []
    node, depth = _resolve(tokens)
    name, rest = " ".join(tokens[:depth]), tokens[depth:]
    found: list[str] = []

    if isinstance(node, click.Group):
        # Stopped on a group: either a bare mention, a `<placeholder>` for the
        # subcommand, or a subcommand name that no longer exists.
        following = rest[0] if rest else ""
        if re.fullmatch(r"[a-z][a-z0-9-]*", following):
            found.append(f"{source}:{lineno}: `{name}` has no subcommand {following!r} — {command}")
        return found

    flags, valued = {"--help"}, set()
    for param in node.params:
        if isinstance(param, click.Option):
            target = flags if (param.is_flag or param.count) else valued
            target.update(param.opts + param.secondary_opts)
    accepts = sum(
        9 if param.nargs == -1 else param.nargs
        for param in node.params
        if isinstance(param, click.Argument)
    )

    positional = 0
    remaining = iter(rest)
    for token in remaining:
        if token.startswith("-") and token != "-":
            option = token.split("=", 1)[0]
            if option in flags:
                continue
            if option in valued:
                if "=" not in token:
                    next(remaining, None)  # skip the option's value
                continue
            found.append(f"{source}:{lineno}: `{name}` has no option {option} — {command}")
        else:
            positional += 1
    if positional > accepts:
        found.append(
            f"{source}:{lineno}: `{name}` takes {accepts} positional arg(s), "
            f"doc passes {positional} — {command}"
        )
    return found


def _guidance_files() -> list[Path]:
    """Every static document that hands an agent a command line to run."""
    return sorted(SKILLS_DIR.glob("*/SKILL.md")) + sorted(PRIME_TEMPLATES_DIR.rglob("*.md"))


def _label(path: Path) -> str:
    if path.name == "SKILL.md":
        return f"src/skills/{path.parent.name}/SKILL.md"
    return f"src/prime/templates/{path.relative_to(PRIME_TEMPLATES_DIR)}"


def test_both_guidance_trees_are_where_we_think_they_are():
    """A moved tree must not turn the scan below into a silent no-op."""
    labels = [_label(path) for path in _guidance_files()]
    assert any(label.startswith("src/skills/") for label in labels), SKILLS_DIR
    assert any(label.startswith("src/prime/templates/") for label in labels), PRIME_TEMPLATES_DIR


@pytest.mark.parametrize("path", _guidance_files(), ids=_label)
def test_every_documented_aq_invocation_matches_the_click_signature(path: Path):
    source = _label(path)
    found: list[str] = []
    for lineno, command in _invocations(path.read_text(encoding="utf-8")):
        found += _problems(source, lineno, command)
    assert not found, (
        "static guidance does not match the real CLI:\n  "
        + "\n  ".join(found)
        + "\n\nRun `aq <group> <cmd> --help` and correct the doc. Auto-generated "
        "commands take `--task-id`, not a positional id; `--json` / `--brief` are "
        "options on the top-level `aq` group and go before the subcommand."
    )


def test_the_guard_catches_each_shape_it_exists_to_catch():
    """The scan is only worth having if these three regressions fail it."""
    stale_subcommand = _problems("x.md", 1, "aq task tree <task_id>")
    assert stale_subcommand and "no subcommand 'tree'" in stale_subcommand[0]

    positional_id = _problems("x.md", 1, "aq task children <id>")
    assert positional_id and "takes 0 positional arg(s), doc passes 1" in positional_id[0]

    misplaced_global = _problems("x.md", 1, "aq task list --json --brief")
    assert [p for p in misplaced_global if "has no option --json" in p]
    assert [p for p in misplaced_global if "has no option --brief" in p]


def test_the_guard_accepts_the_corrected_forms():
    """And stays quiet on the shapes the skills now document."""
    for command in (
        "aq task children --task-id <id> [--recursive] [--status S] [--limit N]",
        "aq task get-tree --task-id <task_id>",
        "aq --json --brief task list",
        "aq task show <task_id>",
        'aq task close <id> --outcome pass --summary "done"',
        "aq task add-dependency --task-id <id> --depends-on <up> --dep-type blocks",
        "aq task list --status IN_PROGRESS   # filter by status",
        "aq --json task list | jq '.[] | select(.status==\"READY\") | .id'",
        "aq task",  # a bare group mention
        "aq task <subcommand>",  # a placeholder subcommand
    ):
        assert _problems("x.md", 1, command) == [], command


def test_invocations_are_read_from_shell_blocks_and_inline_code_only():
    doc = textwrap.dedent(
        """\
        Run `aq task show <id>` before writing.

        ```bash
        aq task close <id> \\
          --outcome pass --summary "..."
        ```

        ```
        aq task       — not a command line, just a group listing
        ```

        Plain prose naming aq task list without backticks is ignored.
        """
    )
    found = _invocations(doc)
    assert found == [
        (1, "aq task show <id>"),
        (4, 'aq task close <id> --outcome pass --summary "..."'),
    ]
