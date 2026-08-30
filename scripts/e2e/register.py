#!/usr/bin/env python3
"""Register the e2e projects and their workspaces against a running daemon.

Projects live in the database, not on disk, so this cannot be part of
``e2e-env.sh``'s build step — it needs the daemon up.  ``e2e-daemon.sh
start`` runs it automatically once the API answers, so both tiers find
``e2e`` and ``other`` already registered: Tier 1's scenarios assume them,
and a Tier 2 operator has no smoke run to create them.

Idempotent: an existing project or workspace is left alone.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smoke import (  # noqa: E402
    OTHER_PROJECT,
    PROJECT,
    CliError,
    Failure,
    aq,
    ensure_project,
    workspace_paths,
)


def main() -> int:
    try:
        for project_id in (PROJECT, OTHER_PROJECT):
            ensure_project(project_id, workspace_paths(project_id))
            count = len(
                aq("project", "list-workspaces", "--project-id", project_id).get(
                    "workspaces", []
                )
            )
            print(f"registered project {project_id!r} with {count} workspace(s)")
    except (Failure, CliError) as exc:
        print(f"registration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
