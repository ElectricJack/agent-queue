#!/usr/bin/env python3
"""Run *this worktree's* ``aq`` CLI, whatever is pip-installed elsewhere.

Two problems this solves, both of which bite the e2e kit:

1. ``aq`` on ``PATH`` resolves ``src`` through the editable install, which
   may point at a different checkout than the worktree under test.
2. ``python3 -m src.cli.app`` loads ``src/cli/app.py`` twice — once as
   ``__main__`` and again as ``src.cli.app`` when ``from .app import cli``
   runs inside ``src/cli/doctor.py`` & friends.  The hand-written groups
   (``doctor``, ``session``, ``formula``, …) then register on the *other*
   module's ``cli`` object and vanish from ``--help``.

Running this file as ``__main__`` avoids both: the repo root goes on
``sys.path`` first, and ``src.cli.app`` is imported exactly once under its
real name.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.path and sys.path[0] != _REPO:
    sys.path.insert(0, _REPO)

from src.cli.app import main  # noqa: E402

if __name__ == "__main__":
    sys.argv[0] = "aq"
    sys.exit(main())
