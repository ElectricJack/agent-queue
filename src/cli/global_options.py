"""Position-independent global options for the ``aq`` CLI.

The design spec (``docs/specs/design/aq-surface.md`` §4) and every skill and
example that ships with the daemon write the global output flags *after* the
command — ``aq status --json``, ``aq task list --brief``.  Click, however,
parses options against the command they follow, so with ``--json`` declared
only on the root group ``aq status --json`` failed with ``no such option``
while ``aq --json status`` worked.  That split is invisible from the usage
text and cost agents a retry every time.

The canonical grammar is therefore: **a global option may appear at any
position** — before the group, between a group and its subcommand, or after
the leaf command and its arguments — and every position means the same
thing.  This module implements that by copying the global options onto every
command and group in the tree (:func:`install_global_options`), with each
copy writing straight into the root context's ``obj`` dict that
``envelope.emit`` and friends already read.

Two deliberate exclusions, both about not stealing a flag from someone else:

* **Passthrough commands** (``aq test``, ``aq stream start`` — anything with
  ``ignore_unknown_options``): their trailing argv belongs to a child
  program, so ``aq test tests/x.py --json`` must hand ``--json`` to pytest.
  Use the prefix form (``aq --json test …``) for those.
* **Commands that already declare an option of the same name** (``aq
  doctor --json``, ``aq logs --json``, ``aq system config get --json``): the local
  option keeps its local meaning and is left untouched.

Everything after a ``--`` separator is likewise left alone — that is Click's
own end-of-options boundary and this module does not change it.
"""

from __future__ import annotations

import click

__all__ = [
    "GLOBAL_OPTION_NAMES",
    "AQGroup",
    "global_params",
    "install_global_options",
    "is_passthrough",
]

#: Long forms of the options this module keeps consistent across positions.
GLOBAL_OPTION_NAMES: tuple[str, ...] = ("--json", "--brief", "--api-url")


def _record(key: str):
    """Build a Click callback that stores *value* on the root context.

    Only truthy values are recorded: a flag left at its default on the leaf
    must not clobber the same flag given in prefix position (``aq --json
    task list`` parses the root's ``--json`` as ``True`` and then the leaf's
    as ``False``).
    """

    def callback(ctx: click.Context, param: click.Parameter, value):
        if value is None or value is False:
            return value
        root = ctx.find_root()
        root.ensure_object(dict)
        root.obj[key] = value
        if ctx.obj is not None and ctx.obj is not root.obj:
            ctx.obj[key] = value
        return value

    return callback


def global_params() -> list[click.Parameter]:
    """Fresh copies of the global options, for one command.

    A ``click.Parameter`` carries no per-invocation state, but building new
    ones per command keeps the objects from being shared across the tree and
    lets each command's help render them independently.

    ``--api-url`` deliberately carries **no** ``envvar`` here: the root group
    reads ``AGENT_QUEUE_API_URL`` already, and a second envvar read on the
    leaf would let the environment override an explicit ``aq --api-url X``
    given in prefix position.
    """
    return [
        click.Option(
            ["--json", "output_json"],
            is_flag=True,
            default=False,
            expose_value=False,
            is_eager=True,
            callback=_record("json"),
            help="Global: output the versioned JSON envelope instead of tables.",
        ),
        click.Option(
            ["--brief"],
            is_flag=True,
            default=False,
            expose_value=False,
            is_eager=True,
            callback=_record("brief"),
            help="Global: trim output to each entity's lite projection.",
        ),
        click.Option(
            ["--api-url"],
            default=None,
            expose_value=False,
            is_eager=True,
            callback=_record("api_url"),
            help="Global: daemon API URL (default: from config or http://127.0.0.1:8081).",
        ),
    ]


def is_passthrough(cmd: click.Command) -> bool:
    """True when *cmd* forwards unrecognised argv to a child program."""
    settings = cmd.context_settings or {}
    return bool(settings.get("ignore_unknown_options"))


def _declared_option_names(cmd: click.Command) -> set[str]:
    names: set[str] = set()
    for param in cmd.params:
        for opt in list(param.opts) + list(param.secondary_opts):
            names.add(opt)
            # ``--json/--no-json`` style flags declare both in one string.
            for piece in opt.split("/"):
                names.add(piece)
    return names


def _install_on(cmd: click.Command) -> None:
    if is_passthrough(cmd):
        return
    declared = _declared_option_names(cmd)
    for param in global_params():
        if any(opt in declared for opt in param.opts):
            continue
        cmd.params.append(param)


#: Set on a group once :func:`install_global_options` has walked it.
_INSTALLED_ATTR = "_aq_globals_installed"


class AQGroup(click.Group):
    """A group that keeps the global options installed on late arrivals.

    ``app.py`` walks the finished tree once, but import order can put a
    registration *after* that walk: importing ``src.cli.daemon`` first makes
    it enter ``app.py`` through the circular ``from .app import cli``, so
    ``app.py`` finishes — install included — while ``daemon.py`` has not yet
    reached its ``@cli.command("start")``.  (The same import cycle is what
    once cost the CLI its real ``aq logs``; see
    ``tests/test_cli_logs.py::test_logs_command_survives_direct_daemon_import``.)
    Installing again on ``add_command`` makes the grammar independent of
    import order.

    ``group_class = type`` makes subgroups created with ``@cli.group()``
    instances of this class too.
    """

    group_class = type

    def add_command(self, cmd: click.Command, name: str | None = None) -> None:
        super().add_command(cmd, name)
        if getattr(self, _INSTALLED_ATTR, False):
            if isinstance(cmd, click.Group):
                _install_on(cmd)
                install_global_options(cmd)
            else:
                _install_on(cmd)


def install_global_options(group: click.Group, *, _seen: set[int] | None = None) -> None:
    """Copy the global options onto every command reachable from *group*.

    Call this after **all** command modules, the auto-generated commands and
    the plugin CLI groups have registered.  Re-running it is harmless: a
    command that already declares an option keeps it and is skipped, which is
    also what lets :class:`AQGroup` re-run it for a late registration.
    """
    seen = _seen if _seen is not None else set()
    if id(group) in seen:
        return
    seen.add(id(group))
    try:
        setattr(group, _INSTALLED_ATTR, True)
    except AttributeError:  # pragma: no cover - defensive, groups are plain objects
        pass

    ctx = click.Context(group)
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None or id(cmd) in seen:
            continue
        if isinstance(cmd, click.Group):
            _install_on(cmd)
            install_global_options(cmd, _seen=seen)
        else:
            seen.add(id(cmd))
            _install_on(cmd)
