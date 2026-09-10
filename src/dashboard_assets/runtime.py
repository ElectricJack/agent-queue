"""Locate and verify the dashboard bundled with an AQ distribution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles


_MANIFEST_NAME = "aq-dashboard-manifest.json"


@dataclass(frozen=True)
class DashboardBundle:
    """A verified dashboard bundle available to the installed daemon."""

    directory: Path
    version: str
    files: dict[str, str]


def installed_version() -> str:
    """Return the installed distribution version without requiring a checkout."""
    try:
        return metadata.version("agent-queue")
    except metadata.PackageNotFoundError:  # pragma: no cover - source-only use
        from src.cli import __version__

        return __version__


def dashboard_directory() -> Path:
    """Return the physical package-data directory used by normal wheel installs."""
    package_root = resources.files("src.dashboard_assets")
    return Path(str(package_root.joinpath("dist")))


def _read_manifest(directory: Path) -> dict[str, Any]:
    try:
        payload = json.loads((directory / _MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"dashboard manifest is unavailable or invalid: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("dashboard manifest has an unsupported schema version")
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        raise ValueError("dashboard manifest has no artifact version")
    if not isinstance(payload.get("files"), dict) or not payload["files"]:
        raise ValueError("dashboard manifest has no file inventory")
    return payload


def verify_dashboard_bundle(directory: Path | None = None) -> DashboardBundle:
    """Validate the packaged dashboard before exposing it over HTTP."""
    directory = directory or dashboard_directory()
    payload = _read_manifest(directory)
    files: dict[str, str] = {}
    for relative, expected_digest in payload["files"].items():
        if not isinstance(relative, str) or not isinstance(expected_digest, str):
            raise ValueError("dashboard manifest contains a non-string file entry")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            raise ValueError(f"dashboard manifest contains unsafe path {relative!r}")
        if len(expected_digest) != 64 or any(char not in "0123456789abcdef" for char in expected_digest):
            raise ValueError(f"dashboard manifest contains an invalid digest for {relative!r}")
        path = directory / candidate
        try:
            actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise ValueError(f"dashboard asset is missing: {relative}") from error
        if actual_digest != expected_digest:
            raise ValueError(f"dashboard asset digest mismatch: {relative}")
        files[relative] = expected_digest
    if "index.html" not in files:
        raise ValueError("dashboard manifest does not include index.html")
    return DashboardBundle(directory=directory, version=payload["version"], files=files)


class _DashboardStaticFiles(StaticFiles):
    """Static files with an index fallback for browser-routed dashboard URLs."""

    async def get_response(self, path: str, scope: Any) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404 and Path(path).suffix == "":
            return await super().get_response("index.html", scope)
        return response


def mount_dashboard(app: FastAPI, *, directory: Path | None = None) -> DashboardBundle | None:
    """Mount a verified release dashboard at ``/dashboard`` when one exists."""
    directory = directory or dashboard_directory()
    if not directory.is_dir():
        # Source checkouts deliberately do not carry generated dashboard output.
        return None
    # A release wheel that has an incomplete or altered package-data tree must
    # fail closed rather than serving an unverified browser application.
    bundle = verify_dashboard_bundle(directory)
    app.mount("/dashboard", _DashboardStaticFiles(directory=str(bundle.directory), html=True), name="dashboard")
    return bundle
