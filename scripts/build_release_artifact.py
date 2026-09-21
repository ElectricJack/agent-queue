#!/usr/bin/env python3
"""Stage the production dashboard and its integrity manifest for a wheel build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path


def project_version(project_root: Path) -> str:
    payload = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = payload.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml has no static project.version")
    return version


#: The URL path the dashboard is built for: the dashboard server serves it at
#: the root, as the Vite dev server does (docs/specs/dashboard-server.md §4).
DASHBOARD_BASE = "/"

#: Build-time escape hatches that point a dashboard at a fixed daemon.  The
#: released page talks only to its own origin (spec §3.1), so they are pinned
#: empty: Vite never lets an ``.env*`` file override a variable that is already
#: set, and every consumer treats an empty value as unset.
DASHBOARD_URL_ESCAPE_HATCHES = ("VITE_API_URL", "VITE_WS_URL", "VITE_TERMINAL_WS_URL")


def build_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment the release dashboard is built with: no ``VITE_*_URL`` override."""
    environment = {
        name: value
        for name, value in environ.items()
        if not (name.startswith("VITE_") and name.endswith("_URL"))
    }
    environment.update(dict.fromkeys(DASHBOARD_URL_ESCAPE_HATCHES, ""))
    return environment


def stage_dashboard(
    source: Path, destination: Path, *, version: str, base: str = DASHBOARD_BASE
) -> dict[str, object]:
    """Copy a built Vite tree and write the exact release file manifest.

    ``base`` records the Vite base the tree was built for; the dashboard
    server refuses a bundle whose manifest does not declare ``"/"``.
    """
    index = source / "index.html"
    if not index.is_file():
        raise ValueError(f"dashboard build is missing {index}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    files: dict[str, str] = {}
    for path in sorted(candidate for candidate in destination.rglob("*") if candidate.is_file()):
        relative = path.relative_to(destination).as_posix()
        if relative != "aq-dashboard-manifest.json":
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest: dict[str, object] = {
        "schema_version": 1,
        "version": version,
        "base": base,
        "files": files,
    }
    (destination / "aq-dashboard-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--source", type=Path, help="Existing dashboard build directory (implies --skip-build)")
    parser.add_argument("--skip-build", action="store_true", help="Stage an existing dashboard/dist tree")
    parser.add_argument("--version", help="Artifact version (defaults to project.version)")
    args = parser.parse_args()
    root = args.project_root.resolve()
    source = args.source.resolve() if args.source else root / "dashboard" / "dist"
    if not (args.skip_build or args.source):
        subprocess.run(
            ["npm", "-w", "dashboard", "run", "build"],
            cwd=root,
            env=build_environment(os.environ),
            check=True,
        )
    manifest = stage_dashboard(
        source,
        root / "src" / "dashboard_assets" / "dist",
        version=args.version or project_version(root),
    )
    print(f"staged dashboard {manifest['version']} with {len(manifest['files'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
