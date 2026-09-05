"""Configuration and diagnostics for project onboarding roots."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.config import (
    AppConfig,
    ConfigValidationError,
    ConfigWatcher,
    ProjectRoot,
    load_config,
    resolve_project_root,
)
from src.doctor.models import DoctorContext, Severity
from src.doctor.project_checks import project_checks
from src.doctor import default_registry
from src.event_bus import EventBus


def _write_config(tmp_path, roots) -> str:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir(exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "messaging_platform": "none",
                "workspace_dir": str(workspaces),
                "database_path": str(tmp_path / "aq.db"),
                "project_roots": roots,
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def test_load_project_roots_expands_and_canonicalizes_path(tmp_path, monkeypatch):
    root = tmp_path / "roots" / "one"
    root.mkdir(parents=True)
    linked = tmp_path / "linked-root"
    linked.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_config(
        tmp_path,
        [{"id": "one-root", "label": "One", "path": "~/linked-root"}],
    )

    config = load_config(path)

    assert config.project_roots == [
        ProjectRoot(id="one-root", label="One", path=str(root.resolve()))
    ]
    resolved = resolve_project_root(config, "one-root")
    assert resolved is not None
    assert resolved.path == str(root.resolve())
    assert resolved.readable is True
    assert resolve_project_root(config, "unknown") is None


@pytest.mark.parametrize(
    ("roots", "message"),
    [
        (
            [
                {"id": "same", "label": "A", "path": "/tmp"},
                {"id": "same", "label": "B", "path": "/var"},
            ],
            "duplicate id 'same'",
        ),
        (
            [
                {"id": "a", "label": "A", "path": "/tmp"},
                {"id": "b", "label": "B", "path": "/tmp/../tmp"},
            ],
            "duplicate canonical path",
        ),
        ([{"id": "not/a-root", "label": "A", "path": "/tmp"}], "must be URL-safe"),
        ([{"id": "relative", "label": "A", "path": "relative"}], "must be an absolute path"),
        (
            [{"id": "missing", "label": "A", "path": "/definitely-not-a-project-root"}],
            "does not exist or is not a directory",
        ),
        ([{"id": "extra", "label": "A", "path": "/tmp", "oops": True}], "exactly id, label, path"),
    ],
)
def test_invalid_project_roots_are_rejected(tmp_path, roots, message):
    with pytest.raises(ConfigValidationError, match=message):
        load_config(_write_config(tmp_path, roots))


@pytest.mark.asyncio
async def test_update_config_round_trips_project_roots_and_comments(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config_path = _write_config(tmp_path, [])
    with open(config_path, "a", encoding="utf-8") as f:
        f.write("# comment outside project roots\n")
    config = load_config(config_path)
    watcher = MagicMock()
    watcher.reload = AsyncMock(return_value={"applied": ["project_roots"]})
    orchestrator = MagicMock(config=config, _config_watcher=watcher)
    from src.commands.handler import CommandHandler

    result = await CommandHandler(orchestrator, config).execute(
        "update_config",
        {
            "section": "project_roots",
            "data": [{"id": "local", "label": "Local", "path": str(root)}],
        },
    )

    assert result["applied"] is True
    assert result["requires_restart"] is False
    written = open(config_path, encoding="utf-8").read()
    assert "# comment outside project roots" in written
    assert load_config(config_path).project_roots[0].path == str(root.resolve())


@pytest.mark.asyncio
async def test_reload_applies_added_and_removed_project_roots(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config_path = _write_config(tmp_path, [])
    bus = EventBus()
    watcher = ConfigWatcher(config_path, bus, load_config(config_path))

    _write_config(tmp_path, [{"id": "local", "label": "Local", "path": str(root)}])
    result = await watcher.reload()
    assert result["applied"] == ["project_roots"]
    assert watcher.config.project_roots[0].id == "local"

    _write_config(tmp_path, [])
    result = await watcher.reload()
    assert result["applied"] == ["project_roots"]
    assert watcher.config.project_roots == []


@pytest.mark.asyncio
async def test_doctor_reports_root_that_is_no_longer_readable(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    config = AppConfig(project_roots=[ProjectRoot(id="local", label="Local", path=str(root))])
    check = project_checks()[0]

    assert (await check.run(DoctorContext(config=config))).severity is Severity.OK
    monkeypatch.setattr("src.config.os.access", lambda _path, mode: mode != 4)
    result = await check.run(DoctorContext(config=config))

    assert result.severity is Severity.ERROR
    assert result.id == "projects.roots"
    assert result.data["roots"][0]["readable"] is False


def test_project_roots_doctor_check_is_registered():
    assert default_registry().get("projects.roots") is not None
