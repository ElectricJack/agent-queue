"""Release artifact staging and the daemon's interim dashboard mount.

The bundle verifier and the static app that serves it are covered in
``tests/test_dashboard_server_bundle.py``.  The ``mount_dashboard`` cases here
go when the mount itself does (smart-meadow.4).
"""

from __future__ import annotations

import importlib.util
import json
from importlib import metadata
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dashboard_assets.runtime import mount_dashboard

ROOT = Path(__file__).resolve().parent.parent


def _release_builder():
    spec = importlib.util.spec_from_file_location(
        "build_release_artifact", ROOT / "scripts" / "build_release_artifact.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(tmp_path: Path) -> Path:
    source = tmp_path / "vite-dist"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text('<script src="assets/app.js"></script>', encoding="utf-8")
    (source / "assets" / "app.js").write_text("console.log('AQ')", encoding="utf-8")
    destination = tmp_path / "package-data"
    manifest = _release_builder().stage_dashboard(source, destination, version="1.2.3")
    assert manifest["version"] == "1.2.3"
    return destination


def test_release_staging_writes_a_versioned_inventory_for_the_root_base(tmp_path):
    destination = _stage(tmp_path)
    manifest = json.loads((destination / "aq-dashboard-manifest.json").read_text())

    # schema_version stays 1 so a pre-change verifier still reads it (spec §6.2).
    assert manifest["schema_version"] == 1
    assert manifest["base"] == "/"
    assert set(manifest["files"]) == {"assets/app.js", "index.html"}
    assert "aq-dashboard-manifest.json" not in manifest["files"]


def test_the_release_build_pins_every_dashboard_url_escape_hatch_empty():
    """The released page talks only to its own origin (spec §3.1)."""
    builder = _release_builder()
    environment = builder.build_environment(
        {"PATH": "/usr/bin", "VITE_API_URL": "http://elsewhere:8081", "VITE_OTHER_URL": "x"}
    )

    assert environment["PATH"] == "/usr/bin"
    assert "VITE_OTHER_URL" not in environment
    for name in ("VITE_API_URL", "VITE_WS_URL", "VITE_TERMINAL_WS_URL"):
        assert environment[name] == ""
    assert "AQ_DASHBOARD_EMBEDDED" not in environment


def test_aq_version_uses_the_installed_distribution_metadata():
    from src.cli.app import _installed_version

    assert _installed_version() == metadata.version("agent-queue")


def test_the_interim_mount_refuses_a_modified_asset(tmp_path):
    destination = _stage(tmp_path)
    (destination / "assets" / "app.js").write_text("altered", encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch: assets/app.js"):
        mount_dashboard(FastAPI(), directory=destination)


def test_the_interim_mount_still_serves_a_bundle_that_predates_base(tmp_path):
    """An install's old bundle must not crash the daemon before it is rebuilt."""
    destination = _stage(tmp_path)
    manifest_path = destination / "aq-dashboard-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["base"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    bundle = mount_dashboard(FastAPI(), directory=destination)

    assert bundle is not None and bundle.base is None


def test_installed_dashboard_serves_assets_and_browser_routes(tmp_path):
    destination = _stage(tmp_path)
    app = FastAPI()
    bundle = mount_dashboard(app, directory=destination)

    assert bundle is not None
    with TestClient(app) as client:
        assert client.get("/dashboard/").status_code == 200
        assert client.get("/dashboard/assets/app.js").text == "console.log('AQ')"
        route = client.get("/dashboard/projects/example/graph")
    assert route.status_code == 200
    assert "assets/app.js" in route.text


def test_the_bare_dashboard_path_survives_the_daemons_catch_all_mount(tmp_path):
    """The daemon mounts its MCP app at "/" after the dashboard.

    A mount at /dashboard only matches /dashboard/..., so a bare /dashboard fell
    through to that catch-all and answered 404: the first macOS install built
    the dashboard, restarted the daemon, and still reported it unreachable.
    """
    from starlette.applications import Starlette
    from starlette.routing import Mount

    destination = _stage(tmp_path)
    app = FastAPI()
    mount_dashboard(app, directory=destination)
    app.router.routes.append(Mount("/", app=Starlette()))  # as src/embedded_mcp.py does

    with TestClient(app) as client:
        bare = client.get("/dashboard", follow_redirects=False)
        assert bare.status_code == 307
        assert bare.headers["location"] == "/dashboard/"
        assert (
            client.get("/dashboard?tab=metrics", follow_redirects=False).headers["location"]
            == "/dashboard/?tab=metrics"
        )
        assert "assets/app.js" in client.get("/dashboard").text
    assert "/dashboard" not in app.openapi()["paths"]


def test_the_core_install_can_serve_websockets():
    """uvicorn serves WebSockets only when a WebSocket library is installed.

    A `pip install -e .[cli]` install -- what the one-command bootstrap does --
    once had none: `websockets` arrived only through the `llm` extra, so the
    dashboard's events stream and every agent terminal failed to connect while
    plain HTTP worked.  The library must be a core dependency, not a lucky
    transitive one.
    """
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core = [
        dependency.split(";")[0].strip().lower()
        for dependency in pyproject["project"]["dependencies"]
    ]
    names = {
        dependency.split("[")[0].split("<")[0].split(">")[0].split("=")[0].split("!")[0].strip()
        for dependency in core
    }
    assert names & {"websockets", "wsproto"} or any(
        dependency.startswith("uvicorn[standard]") for dependency in core
    )


def test_release_metadata_declares_runtime_resources_and_the_root_base():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    vite_config = (ROOT / "dashboard" / "vite.config.ts").read_text(encoding="utf-8")
    release_script = (ROOT / "scripts" / "build_release_artifact.py").read_text(encoding="utf-8")
    dashboard_entry = (ROOT / "dashboard" / "src" / "main.tsx").read_text(encoding="utf-8")

    assert '"src.dashboard_assets" = ["dist/**"]' in pyproject
    assert 'src = ["**/*.md", "**/*.json", "**/*.sha256"]' in pyproject
    # One URL shape everywhere: the dev server and the dashboard server both
    # serve the bundle at "/", so there is no embedded build any more (spec §4).
    assert 'base: "/",' in vite_config
    assert "/dashboard/" not in vite_config
    assert "AQ_DASHBOARD_EMBEDDED" not in vite_config
    assert "AQ_DASHBOARD_EMBEDDED" not in release_script
    assert _release_builder().DASHBOARD_BASE == "/"
    assert "<BrowserRouter basename={import.meta.env.BASE_URL}>" in dashboard_entry


def test_versioned_installation_release_record_matches_the_artifact_contract():
    """Keep the public release ledger aligned with the wheel it describes."""
    document = (ROOT / "docs" / "validation" / "installation-release-0.1.0.md").read_text(
        encoding="utf-8"
    )
    version = _release_builder().project_version(ROOT)

    assert f"# AQ {version} installation release record" in document
    assert f"agent_queue-{version}-py3-none-any.whl" in document
    assert "python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist" in document
    assert "`aq install --list-steps --json`" in document
    assert "`agent-queue`" in document
    for relative_link in (
        "../tutorials/install.md",
        "../tutorials/first-task.md",
        "../reference/cli/install.md",
        "../reference/cli/uninstall.md",
        "windows-wsl-onboarding.md",
        "../plans/install-onboarding/acceptance/macos.md",
    ):
        assert relative_link in document
