"""Release artifact staging and installed-dashboard boundaries."""

from __future__ import annotations

import importlib.util
import json
from importlib import metadata
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dashboard_assets.runtime import mount_dashboard, verify_dashboard_bundle


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


def test_release_staging_writes_a_versioned_inventory_that_verifies(tmp_path):
    destination = _stage(tmp_path)
    manifest = json.loads((destination / "aq-dashboard-manifest.json").read_text())

    assert manifest["schema_version"] == 1
    assert set(manifest["files"]) == {"assets/app.js", "index.html"}
    bundle = verify_dashboard_bundle(destination)
    assert bundle.version == "1.2.3"
    assert set(bundle.files) == set(manifest["files"])


def test_aq_version_uses_the_installed_distribution_metadata():
    from src.cli.app import _installed_version

    assert _installed_version() == metadata.version("agent-queue")


def test_dashboard_integrity_check_refuses_a_modified_asset(tmp_path):
    destination = _stage(tmp_path)
    (destination / "assets" / "app.js").write_text("altered", encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch: assets/app.js"):
        verify_dashboard_bundle(destination)
    with pytest.raises(ValueError, match="digest mismatch: assets/app.js"):
        mount_dashboard(FastAPI(), directory=destination)


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


def test_release_metadata_declares_runtime_resources_and_embedded_base():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    vite_config = (ROOT / "dashboard" / "vite.config.ts").read_text(encoding="utf-8")
    dashboard_entry = (ROOT / "dashboard" / "src" / "main.tsx").read_text(encoding="utf-8")

    assert '"src.dashboard_assets" = ["dist/**"]' in pyproject
    assert 'src = ["**/*.md", "**/*.json", "**/*.sha256"]' in pyproject
    assert 'AQ_DASHBOARD_EMBEDDED === "1"' in vite_config
    assert 'base: embedded ? "/dashboard/" : "/"' in vite_config
    assert "<BrowserRouter basename={import.meta.env.BASE_URL}>" in dashboard_entry
