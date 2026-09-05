"""``aq playbook`` — CLI group for playbooks.

The subcommands are auto-generated from the CommandHandler tool registry via
``register_auto_commands``; this module only anchors the group so hand-crafted
subcommands that need custom exit-code / output shaping have a home.
"""
from __future__ import annotations

from .app import cli


@cli.group("playbook")
def playbook_group():
    """Playbook commands."""
