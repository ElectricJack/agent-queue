"""The dashboard server's bundle: fail-closed verification and the static app (spec §4).

The verifier refuses anything it cannot vouch for -- an altered or missing
file, an unsafe path, a bundle without ``index.html``, and a bundle built for
the daemon's old ``/dashboard/`` mount.  The static app serves only what the
manifest lists, falls back to ``index.html`` for browser routes, and marks
every response with the cache and security headers the spec names.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from src.dashboard_server.bundle import (
    BUNDLE_BASE,
    MANIFEST_NAME,
    BundleStaticApp,
    DashboardBundle,
    verify_bundle_inventory,
    verify_dashboard_bundle,
)

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = '<!doctype html><script type="module" src="/assets/app.js"></script>'
APP_JS = "console.log('AQ')"
IMMUTABLE = "public, max-age=31536000, immutable"
SECURITY = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "frame-ancestors 'self'",
}


def _release_builder():
    spec = importlib.util.spec_from_file_location(
        "build_release_artifact", ROOT / "scripts" / "build_release_artifact.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(tmp_path: Path) -> Path:
    """A bundle exactly as the release script stages it."""
    source = tmp_path / "vite-dist"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (source / "assets" / "app.js").write_text(APP_JS, encoding="utf-8")
    (source / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    destination = tmp_path / "package-data"
    _release_builder().stage_dashboard(source, destination, version="1.2.3")
    return destination


def _rewrite_manifest(directory: Path, change: Callable[[dict[str, Any]], None]) -> None:
    path = directory / MANIFEST_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    change(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    return _stage(tmp_path)


@pytest.fixture
def client(staged: Path) -> TestClient:
    return TestClient(BundleStaticApp(verify_dashboard_bundle(staged)))


def _assert_secured(response) -> None:
    for name, value in SECURITY.items():
        assert response.headers[name] == value, name


# -- verification ------------------------------------------------------------


def test_staging_writes_an_inventory_that_verifies(staged: Path):
    bundle = verify_dashboard_bundle(staged)

    assert isinstance(bundle, DashboardBundle)
    assert bundle.version == "1.2.3"
    assert bundle.base == BUNDLE_BASE == "/"
    assert bundle.directory == staged
    assert set(bundle.files) == {"assets/app.js", "favicon.svg", "index.html"}
    assert bundle.files["assets/app.js"] == hashlib.sha256(APP_JS.encode()).hexdigest()


def test_a_modified_asset_is_refused(staged: Path):
    (staged / "assets" / "app.js").write_text("altered", encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch: assets/app.js"):
        verify_dashboard_bundle(staged)
    with pytest.raises(ValueError, match="digest mismatch: assets/app.js"):
        verify_bundle_inventory(staged)


def test_a_missing_asset_is_refused(staged: Path):
    (staged / "assets" / "app.js").unlink()

    with pytest.raises(ValueError, match="asset is missing: assets/app.js"):
        verify_dashboard_bundle(staged)


@pytest.mark.parametrize(
    "unsafe",
    ["../outside.js", "/etc/passwd", "assets/../index.html", "./index.html", "a\\b.js", "a//b.js"],
)
def test_an_unsafe_path_is_refused(staged: Path, unsafe: str):
    digest = "0" * 64
    _rewrite_manifest(staged, lambda manifest: manifest["files"].__setitem__(unsafe, digest))

    with pytest.raises(ValueError, match="unsafe path"):
        verify_dashboard_bundle(staged)


def test_a_listed_file_that_links_outside_the_bundle_is_refused(staged: Path, tmp_path: Path):
    outside = tmp_path / "outside.js"
    outside.write_text("secret", encoding="utf-8")
    (staged / "assets" / "link.js").symlink_to(outside)
    digest = hashlib.sha256(b"secret").hexdigest()
    _rewrite_manifest(
        staged, lambda manifest: manifest["files"].__setitem__("assets/link.js", digest)
    )

    with pytest.raises(ValueError, match="escapes the bundle directory: assets/link.js"):
        verify_dashboard_bundle(staged)


def test_a_bundle_without_index_html_is_refused(staged: Path):
    _rewrite_manifest(staged, lambda manifest: manifest["files"].pop("index.html"))

    with pytest.raises(ValueError, match="does not include index.html"):
        verify_dashboard_bundle(staged)


def test_an_invalid_digest_string_is_refused(staged: Path):
    _rewrite_manifest(
        staged, lambda manifest: manifest["files"].__setitem__("assets/app.js", "A" * 64)
    )

    with pytest.raises(ValueError, match="invalid digest for 'assets/app.js'"):
        verify_dashboard_bundle(staged)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda manifest: manifest.__setitem__("schema_version", 2), "unsupported schema version"),
        (lambda manifest: manifest.__setitem__("version", ""), "no artifact version"),
        (lambda manifest: manifest.__setitem__("files", {}), "no file inventory"),
    ],
)
def test_a_malformed_manifest_is_refused(staged: Path, change, message: str):
    _rewrite_manifest(staged, change)

    with pytest.raises(ValueError, match=message):
        verify_dashboard_bundle(staged)


def test_a_missing_manifest_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="manifest is unavailable or invalid"):
        verify_dashboard_bundle(tmp_path)


def test_a_bundle_built_for_the_daemon_mount_is_refused_with_the_rebuild_command(staged: Path):
    """Served at "/", a `/dashboard/`-based page would load with every asset 404ing."""
    _rewrite_manifest(staged, lambda manifest: manifest.pop("base"))

    with pytest.raises(ValueError, match="built for the daemon mount; rebuild") as refused:
        verify_dashboard_bundle(staged)
    assert "aq install --restart-from dashboard.build" in str(refused.value)
    # The interim daemon mount still accepts it, so the daemon keeps starting.
    assert verify_bundle_inventory(staged).base is None


def test_a_bundle_built_for_another_base_is_refused(staged: Path):
    _rewrite_manifest(staged, lambda manifest: manifest.__setitem__("base", "/dashboard/"))

    with pytest.raises(ValueError, match="declares base '/dashboard/'"):
        verify_dashboard_bundle(staged)
    assert verify_bundle_inventory(staged).base == "/dashboard/"


# -- serving -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/tasks", "/tasks/abc", "/projects/x/graph", "/tasks/"])
def test_browser_routes_get_index_html(client: TestClient, path: str):
    response = client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX_HTML
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    _assert_secured(response)


def test_index_html_is_served_by_name_and_revalidated(client: TestClient):
    response = client.get("/index.html")

    assert response.status_code == 200
    assert response.text == INDEX_HTML
    assert response.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable(client: TestClient):
    response = client.get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == APP_JS
    assert "javascript" in response.headers["content-type"]
    assert response.headers["cache-control"] == IMMUTABLE
    assert response.headers["etag"]
    assert response.headers["last-modified"]
    _assert_secured(response)


def test_other_listed_files_are_revalidated(client: TestClient):
    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/nope.js", "/assets/nope.js", "/tasks/v1.2"])
def test_an_unlisted_path_with_a_suffix_is_404(client: TestClient, path: str):
    response = client.get(path)

    assert response.status_code == 404
    assert response.text == "Not Found"
    _assert_secured(response)


def test_a_file_on_disk_outside_the_manifest_is_never_served(staged: Path):
    app = BundleStaticApp(verify_dashboard_bundle(staged))
    (staged / "secret.txt").write_text("secret", encoding="utf-8")
    (staged / "assets" / "extra.js").write_text("secret", encoding="utf-8")
    (staged / "secret").write_text("secret", encoding="utf-8")

    client = TestClient(app)

    assert client.get("/secret.txt").status_code == 404
    assert client.get("/assets/extra.js").status_code == 404
    assert client.get(f"/{MANIFEST_NAME}").status_code == 404
    # Extensionless and unlisted is a browser route: the app shell, not the file.
    extensionless = client.get("/secret")
    assert extensionless.status_code == 200
    assert extensionless.text == INDEX_HTML


def test_a_listed_file_removed_after_verification_is_404_not_500(staged: Path):
    app = BundleStaticApp(verify_dashboard_bundle(staged))
    (staged / "assets" / "app.js").unlink()

    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/assets/app.js").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/outside",
        "/%2e%2e/outside.txt",
        "/assets/%2e%2e/%2e%2e/outside",
        "/assets/%2e%2e/app.js",
        "/%2e/index.html",
        "/..%2Foutside",
        "/..%5Coutside",
        "/assets%5Capp.js",
        "/index.html%00",
        "/tasks%00",
        "/%2Fetc/passwd",
    ],
)
def test_traversal_attempts_are_404(staged: Path, tmp_path: Path, path: str):
    (tmp_path / "outside").write_text("outside", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    client = TestClient(BundleStaticApp(verify_dashboard_bundle(staged)))

    response = client.get(path)

    assert response.status_code == 404
    assert "outside" not in response.text
    _assert_secured(response)


async def _call(app: BundleStaticApp, path: str, method: str = "GET") -> tuple[int, dict, bytes]:
    """Drive the app with a raw scope, as a server that does not normalise paths would."""
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1", "replace"),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8082")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8082),
    }
    await app(scope, receive, send)
    start = messages[0]
    headers = {name.decode().lower(): value.decode() for name, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in messages[1:])
    return start["status"], headers, body


@pytest.mark.parametrize(
    "path",
    ["/../outside", "/assets/../../outside", "/./index.html", "/tasks/./x", "//outside", "tasks"],
)
async def test_literal_dot_segments_and_absolute_paths_are_404(staged: Path, path: str):
    status, headers, body = await _call(BundleStaticApp(verify_dashboard_bundle(staged)), path)

    assert status == 404
    assert body == b"Not Found"
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_only_get_and_head_are_allowed(client: TestClient, method: str):
    response = client.request(method, "/")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    _assert_secured(response)


def test_head_answers_headers_without_a_body(client: TestClient):
    response = client.head("/assets/app.js")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len(APP_JS))
    assert response.headers["cache-control"] == IMMUTABLE
    _assert_secured(response)
    fallback = client.head("/tasks")
    assert fallback.status_code == 200 and fallback.headers["cache-control"] == "no-cache"


def test_a_revalidated_file_is_304_with_its_headers(client: TestClient):
    etag = client.get("/").headers["etag"]

    response = client.get("/tasks", headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["cache-control"] == "no-cache"
    _assert_secured(response)


def test_a_websocket_is_refused(client: TestClient):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/events"):
        pass


# -- import boundary (§1) and the pre-change updater's import (§6.2) --------------

_FORBIDDEN = ("fastapi", "src.api", "src.orchestrator", "src.database", "src.commands")

_PROBE = """
import json, sys
import {module} as probed
forbidden = {forbidden!r}
loaded = sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
import src.dashboard_assets.runtime as runtime
import src.dashboard_server.bundle as bundle
print(json.dumps({{
    "loaded": loaded,
    "same_verifier": runtime.verify_dashboard_bundle is bundle.verify_dashboard_bundle,
    "file": probed.__file__,
}}))
"""


@pytest.mark.parametrize("module", ["src.dashboard_server.bundle", "src.dashboard_assets.runtime"])
def test_importing_the_bundle_loads_nothing_of_the_daemon(module: str):
    """The installer and a pre-change `aq update` import this; neither may pull in FastAPI.

    ``sys.modules`` is read straight after importing the module under test, in
    a fresh interpreter; the other is imported afterwards only to compare the
    re-export.
    """
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PYTHON")
    }
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module, forbidden=_FORBIDDEN)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["loaded"] == []
    # A pre-change updater lazy-imports this exact name from the new checkout.
    assert report["same_verifier"] is True
    assert Path(report["file"]).resolve().is_relative_to(ROOT)
