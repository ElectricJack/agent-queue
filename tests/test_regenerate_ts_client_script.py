"""TypeScript generation must use the checkout's locked tools, never npx's cache."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("@hey-api/openapi-ts", "typescript", "@hey-api/client-fetch")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    if shutil.which("node") is None:
        pytest.skip("Node is required to exercise the TypeScript generator preflight")
    root = tmp_path / "checkout with spaces"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts/regenerate-ts-client.sh", root / "scripts")
    client = root / "packages/aq-ts-client"
    client.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "packages/aq-ts-client/package.json", client)
    (root / "openapi.json").write_text('{"paths": {}}', encoding="utf-8")
    # Ancestor packages and PATH tools must not rescue an uninstalled checkout.
    _install_tools(tmp_path)
    return root


def _install_tools(root: Path) -> None:
    manifest = json.loads((REPO_ROOT / "packages/aq-ts-client/package.json").read_text())
    for name in TOOLS:
        package = root / "node_modules" / name
        package.mkdir(parents=True)
        version = manifest["devDependencies"].get(name) or manifest["dependencies"][name]
        (package / "package.json").write_text(json.dumps({"version": version}))
    cli = root / "node_modules/@hey-api/openapi-ts/bin/index.cjs"
    cli.parent.mkdir()
    cli.write_text(
        "require('node:fs').writeFileSync(process.env.GENERATOR_LOG, "
        "JSON.stringify(process.argv.slice(2)));\n",
        encoding="utf-8",
    )


def _run(root: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("npx", "openapi-ts"):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\necho 'unexpected cached/PATH tool' >&2\nexit 99\n")
        tool.chmod(0o755)
    return subprocess.run(
        ["bash", str(root / "scripts/regenerate-ts-client.sh"), "--from-file"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GENERATOR_LOG": str(tmp_path / "generator.json"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_missing_local_install_does_not_fall_back_to_ancestor_or_cache(
    checkout: Path,
    tmp_path: Path,
):
    result = _run(checkout, tmp_path)

    assert result.returncode == 1
    assert "found missing" in result.stderr
    assert f'npm ci --prefix "{checkout}"' in result.stderr
    assert not (tmp_path / "generator.json").exists()
    assert not (checkout / "packages/aq-ts-client/src").exists()


@pytest.mark.parametrize("name", TOOLS)
def test_stale_toolchain_fails_before_generation(checkout: Path, tmp_path: Path, name: str):
    _install_tools(checkout)
    (checkout / "node_modules" / name / "package.json").write_text('{"version": "99.0.0"}')

    result = _run(checkout, tmp_path)

    assert result.returncode == 1
    assert name in result.stderr
    assert "found 99.0.0" in result.stderr
    assert "npm ci" in result.stderr
    assert not (tmp_path / "generator.json").exists()


def test_installed_generator_runs_from_another_directory(checkout: Path, tmp_path: Path):
    _install_tools(checkout)

    result = _run(checkout, tmp_path)

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "generator.json").read_text()) == [
        "--input",
        str(checkout / "openapi.json"),
        "--output",
        str(checkout / "packages/aq-ts-client/src"),
        "--client",
        "@hey-api/client-fetch",
    ]


def test_workspace_toolchain_pins_match_the_lockfile():
    manifest = json.loads((REPO_ROOT / "packages/aq-ts-client/package.json").read_text())
    packages = json.loads((REPO_ROOT / "package-lock.json").read_text())["packages"]
    workspace = packages["packages/aq-ts-client"]
    for name in TOOLS:
        group = "devDependencies" if name in manifest["devDependencies"] else "dependencies"
        assert manifest[group][name] == workspace[group][name]
        assert manifest[group][name] == packages[f"node_modules/{name}"]["version"]
