"""The verified dashboard bundle and the static app that serves it (spec §4).

A release stages the built SPA into ``src/dashboard_assets/dist`` together with
``aq-dashboard-manifest.json``: the artifact version, the Vite ``base`` it was
built for, and the SHA-256 of every file.  Nothing is served until every listed
file hashes to its recorded digest, and only listed files are ever served.

Import boundary (§1): the standard library, Starlette and :mod:`src.config`
only -- no ``fastapi``, ``src.api``, ``src.orchestrator``, ``src.database`` or
``src.commands``.  The installer and a pre-change ``aq update`` import the
verifier from here (through :mod:`src.dashboard_assets.runtime`), so importing
this module must stay cheap.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate
from importlib import metadata, resources
from pathlib import Path, PurePosixPath
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.staticfiles import NotModifiedResponse
from starlette.types import Message, Receive, Scope, Send

MANIFEST_NAME = "aq-dashboard-manifest.json"
#: The URL path the bundle is built for and served at.
BUNDLE_BASE = "/"

REBUILD_COMMAND = "aq install --restart-from dashboard.build"

INDEX = "index.html"
_HEX = frozenset("0123456789abcdef")

#: Cache policy.  ``index.html`` names the content-hashed assets of *this*
#: build, so a browser must revalidate it; the hashed assets never change.
CACHE_REVALIDATE = "no-cache"
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
_IMMUTABLE_PREFIX = "assets/"

#: Carried by every response this app sends, errors included.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("content-security-policy", "frame-ancestors 'self'"),
)
_ALLOWED_METHODS = "GET, HEAD"


@dataclass(frozen=True)
class DashboardBundle:
    """A dashboard bundle whose every listed file matched its recorded digest."""

    directory: Path
    version: str
    #: Relative POSIX path -> SHA-256 hex digest, exactly as the manifest lists them.
    files: dict[str, str]
    #: The manifest's ``base``; ``None`` for a bundle built for the daemon mount.
    base: str | None
    #: SHA-256 of the manifest file itself.  ``version`` is the project version,
    #: which every rebuild of one release shares, so this is what tells a
    #: running server's build from the one installed now (:func:`manifest_sha256`).
    manifest_sha256: str = ""


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


def manifest_sha256(directory: Path | None = None) -> str | None:
    """SHA-256 of the installed manifest's bytes, or ``None`` when there is none.

    The fingerprint of one build: every file's digest is in the manifest, so
    two builds that differ anywhere differ here.  Deliberately not a
    verification -- ``aq status`` and ``aq doctor`` use it to ask "is the
    running server serving what is installed?", and the server verifies what
    it serves when it starts.
    """
    try:
        raw = ((directory or dashboard_directory()) / MANIFEST_NAME).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()


def _read_manifest(directory: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = (directory / MANIFEST_NAME).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"dashboard manifest is unavailable or invalid: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("dashboard manifest has an unsupported schema version")
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        raise ValueError("dashboard manifest has no artifact version")
    if not isinstance(payload.get("files"), dict) or not payload["files"]:
        raise ValueError("dashboard manifest has no file inventory")
    return payload, hashlib.sha256(raw).hexdigest()


def _is_safe_relative(relative: str) -> bool:
    """A canonical, relative POSIX path with no dot segment, backslash or NUL."""
    if not relative or "\\" in relative or "\x00" in relative:
        return False
    if relative.startswith("/") or Path(relative).is_absolute():
        return False
    return all(segment not in ("", ".", "..") for segment in relative.split("/"))


def _verify(directory: Path | None) -> tuple[DashboardBundle, dict[str, Any]]:
    directory = directory or dashboard_directory()
    payload, digest = _read_manifest(directory)
    try:
        root = directory.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"dashboard directory is unavailable: {error}") from error
    files: dict[str, str] = {}
    for relative, expected_digest in payload["files"].items():
        if not isinstance(relative, str) or not isinstance(expected_digest, str):
            # ValueError, not TypeError: every caller treats ValueError as "not verified".
            raise ValueError("dashboard manifest contains a non-string file entry")  # noqa: TRY004
        if not _is_safe_relative(relative):
            raise ValueError(f"dashboard manifest contains unsafe path {relative!r}")
        if len(expected_digest) != 64 or not set(expected_digest) <= _HEX:
            raise ValueError(f"dashboard manifest contains an invalid digest for {relative!r}")
        path = directory / relative
        try:
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError(f"dashboard asset escapes the bundle directory: {relative}")
            actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise ValueError(f"dashboard asset is missing: {relative}") from error
        if actual_digest != expected_digest:
            raise ValueError(f"dashboard asset digest mismatch: {relative}")
        files[relative] = expected_digest
    if INDEX not in files:
        raise ValueError("dashboard manifest does not include index.html")
    base = payload.get("base")
    bundle = DashboardBundle(
        directory=directory,
        version=payload["version"],
        files=files,
        base=base if isinstance(base, str) else None,
        manifest_sha256=digest,
    )
    return bundle, payload


def verify_bundle_inventory(directory: Path | None = None) -> DashboardBundle:
    """Verify the manifest and every listed file, but not the base it was built for.

    Only the daemon's interim ``/dashboard`` mount uses this, so an install whose
    bundle predates ``base`` keeps starting until the mount is removed.
    """
    return _verify(directory)[0]


def verify_dashboard_bundle(directory: Path | None = None) -> DashboardBundle:
    """Validate the packaged dashboard before exposing it over HTTP.

    Fails closed with :class:`ValueError`: an altered, incomplete or unsafe
    inventory, and a bundle built for any base but ``/`` -- including one built
    for the daemon's old ``/dashboard/`` mount, whose manifest has no ``base``
    and whose pages would load with every asset answering 404.
    """
    bundle, payload = _verify(directory)
    if "base" not in payload:
        raise ValueError(
            "dashboard bundle was built for the daemon mount; rebuild it with "
            f"`{REBUILD_COMMAND}` (its manifest declares no base)"
        )
    if payload["base"] != BUNDLE_BASE:
        raise ValueError(
            f"dashboard manifest declares base {payload['base']!r}, but the dashboard "
            f"server serves the bundle at {BUNDLE_BASE!r}; rebuild it with `{REBUILD_COMMAND}`"
        )
    return bundle


def _route_path(scope: Scope) -> str:
    """The request path below the app's mount point (as Starlette's own apps read it)."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if (
        root_path
        and path.startswith(root_path)
        and path != root_path
        and path[len(root_path)] == "/"
    ):
        return path[len(root_path) :]
    return path


def _is_not_modified(response_headers: Mapping[str, str], request_headers: Headers) -> bool:
    if if_none_match := request_headers.get("if-none-match"):
        etag = response_headers.get("etag")
        tags = [tag.strip().removeprefix("W/") for tag in if_none_match.split(",")]
        return etag is not None and etag in tags
    since = request_headers.get("if-modified-since")
    modified = response_headers.get("last-modified")
    if since and modified:
        since_date, modified_date = parsedate(since), parsedate(modified)
        return since_date is not None and modified_date is not None and since_date >= modified_date
    return False


def _cache_control(relative: str) -> str:
    return CACHE_IMMUTABLE if relative.startswith(_IMMUTABLE_PREFIX) else CACHE_REVALIDATE


class BundleStaticApp:
    """ASGI app (http scope only) serving a verified bundle at ``/`` with SPA fallback.

    Only files the manifest lists are ever served: the lookup is membership in
    the verified inventory, never a walk of the directory.  ``/`` is
    ``index.html``; a path with no suffix that is not a listed file is a browser
    route and gets ``index.html``; anything else is ``404``.  Which prefixes
    never reach this app (``/api``, ``/__aq``, ...) is the caller's decision.
    """

    def __init__(self, bundle: DashboardBundle) -> None:
        self.bundle = bundle
        self._files = frozenset(bundle.files)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Closing before accepting denies the handshake (HTTP 403).
            await send({"type": "websocket.close", "code": 1000})
            return
        if scope["type"] != "http":
            return

        async def send_secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                reserved = {name.encode("latin-1") for name, _ in SECURITY_HEADERS}
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in reserved
                ]
                headers.extend(
                    (name.encode("latin-1"), value.encode("latin-1"))
                    for name, value in SECURITY_HEADERS
                )
                message = {**message, "headers": headers}
            await send(message)

        response = await self._respond(scope)
        await response(scope, receive, send_secured)

    async def _respond(self, scope: Scope) -> Response:
        if scope["method"].upper() not in ("GET", "HEAD"):
            return PlainTextResponse(
                "Method Not Allowed", status_code=405, headers={"Allow": _ALLOWED_METHODS}
            )
        relative = self._resolve(_route_path(scope))
        if relative is None:
            return _not_found()
        return await self._file(relative, scope)

    def _resolve(self, path: str) -> str | None:
        """The listed file a request path names, ``index.html`` for a browser route, else None."""
        if not path.startswith("/"):
            return None
        relative = path[1:]
        if relative == "":
            return INDEX
        if "\\" in relative or "\x00" in relative:
            return None
        segments = relative.split("/")
        # A trailing slash leaves one empty last segment; that is still a route.
        inner = segments[:-1] if segments[-1] == "" else segments
        if any(segment in ("", ".", "..") for segment in inner):
            return None
        if relative in self._files:
            return relative
        if PurePosixPath(relative).suffix:
            return None
        return INDEX

    async def _file(self, relative: str, scope: Scope) -> Response:
        path = self.bundle.directory / relative
        try:
            stat_result = await run_in_threadpool(os.stat, path)
        except OSError:
            return _not_found()
        if not stat.S_ISREG(stat_result.st_mode):
            return _not_found()
        response = FileResponse(
            path,
            stat_result=stat_result,
            headers={"Cache-Control": _cache_control(relative)},
        )
        if _is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response


def _not_found() -> Response:
    return PlainTextResponse("Not Found", status_code=404)
