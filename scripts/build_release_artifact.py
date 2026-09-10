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
from pathlib import Path


def project_version(project_root: Path) -> str:
    payload = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = payload.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml has no static project.version")
    return version


def stage_dashboard(source: Path, destination: Path, *, version: str) -> dict[str, object]:
    """Copy a built Vite tree and write the exact release file manifest."""
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
    manifest: dict[str, object] = {"schema_version": 1, "version": version, "files": files}
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
        environment = {**os.environ, "AQ_DASHBOARD_EMBEDDED": "1"}
        subprocess.run(["npm", "-w", "dashboard", "run", "build"], cwd=root, env=environment, check=True)
    manifest = stage_dashboard(
        source,
        root / "src" / "dashboard_assets" / "dist",
        version=args.version or project_version(root),
    )
    print(f"staged dashboard {manifest['version']} with {len(manifest['files'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
