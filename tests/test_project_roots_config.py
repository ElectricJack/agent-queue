"""Configuration and diagnostics for project onboarding roots."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

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
from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.doctor.project_checks import project_checks
from src.event_bus import EventBus
from src.projects.roots import (
    PROJECT_ROOT_ACCESS_REMEDIATION,
    PROJECT_ROOT_REMEDIATION,
    ProjectRootsState,
    RootFacts,
    assess_project_roots,
)


def _write_config(tmp_path, roots) -> str:
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir(exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "messaging_platform": "none",
                "workspace_dir": str(workspaces),
                "database": {"url": "postgresql+asyncpg://test:test@localhost/test"},
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
    watcher = ConfigWatcher(config_path, EventBus(), config)
    orchestrator = MagicMock(config=config, _config_watcher=watcher)
    from src.commands.handler import CommandHandler

    handler = CommandHandler(orchestrator, config)
    result = await handler.execute(
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
    assert await handler.execute("list_project_roots", {}) == {
        "success": True,
        "roots": [
            {
                "id": "local",
                "label": "Local",
                "path": str(root.resolve()),
                "readable": True,
                "writable": True,
            }
        ],
    }

    invalid = await handler.execute(
        "update_config",
        {
            "section": "project_roots",
            "data": [{"id": "missing", "label": "Missing", "path": str(root / "missing")}],
        },
    )
    assert invalid["applied"] is False
    assert await handler.execute("list_project_roots", {}) == {
        "success": True,
        "roots": [
            {
                "id": "local",
                "label": "Local",
                "path": str(root.resolve()),
                "readable": True,
                "writable": True,
            }
        ],
    }

    removed = await handler.execute(
        "update_config", {"section": "project_roots", "data": []}
    )
    assert removed["applied"] is True
    assert await handler.execute("list_project_roots", {}) == {"success": True, "roots": []}


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


@pytest.mark.asyncio
async def test_doctor_warns_when_no_project_root_is_configured():
    """An unconfigured root is a warning, and must not read as OK.

    The installer's closing summary reports "Project root: needs attention"
    for the same fact and then points at `aq doctor`; the two surfaces have to
    agree, and send the operator to the same two places.
    """
    check = project_checks()[0]

    result = await check.run(DoctorContext(config=AppConfig(project_roots=[])))

    assert result.id == "projects.roots"
    assert result.severity is Severity.WARN
    assert "No project root is configured" in result.detail
    assert PROJECT_ROOT_REMEDIATION in result.detail
    assert result.data["roots"] == []


@pytest.mark.asyncio
async def test_doctor_warns_when_the_only_configured_root_is_read_only(tmp_path, monkeypatch):
    """A readable-but-not-writable root is not a place a project can be created.

    `ProjectOnboardingService` refuses a non-writable root for every source
    mode but `link`, and the installer's first-task readiness check reports
    "No configured project root is both readable and writable" for exactly
    this configuration.  Reporting OK here would leave `aq doctor` and the
    installer contradicting each other about one directory's permissions.
    """
    root = tmp_path / "root"
    root.mkdir()
    config = AppConfig(project_roots=[ProjectRoot(id="mounted", label="Mounted", path=str(root))])
    check = project_checks()[0]

    assert (await check.run(DoctorContext(config=config))).severity is Severity.OK
    monkeypatch.setattr("src.config.os.access", lambda _path, mode: mode != os.W_OK)
    result = await check.run(DoctorContext(config=config))

    assert result.severity is Severity.WARN
    assert result.id == "projects.roots"
    assert "readable and writable" in result.detail
    assert str(root) in result.detail, "the offending root is named, not just counted"
    assert PROJECT_ROOT_ACCESS_REMEDIATION in result.detail
    assert result.data["state"] == "unusable"
    assert result.data["roots"][0]["writable"] is False


@pytest.mark.asyncio
async def test_doctor_is_ok_when_one_of_several_roots_can_take_a_project(tmp_path, monkeypatch):
    """One usable root is enough; a second read-only root does not degrade it."""
    usable = tmp_path / "usable"
    usable.mkdir()
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    config = AppConfig(
        project_roots=[
            ProjectRoot(id="readonly", label="Read only", path=str(readonly)),
            ProjectRoot(id="usable", label="Usable", path=str(usable)),
        ]
    )
    check = project_checks()[0]
    monkeypatch.setattr(
        "src.config.os.access",
        lambda path, mode: not (mode == os.W_OK and path == str(readonly)),
    )

    result = await check.run(DoctorContext(config=config))

    assert result.severity is Severity.OK
    assert "1 of 2" in result.detail
    assert "usable" in result.detail


def test_project_root_remediation_names_both_operator_surfaces():
    """Same destinations the installation wizard's readiness check names."""
    assert "Settings → Project Roots" in PROJECT_ROOT_REMEDIATION
    assert "`project_roots:`" in PROJECT_ROOT_REMEDIATION
    assert "permissions" in PROJECT_ROOT_ACCESS_REMEDIATION



@pytest.mark.parametrize(
    ("roots", "expected"),
    [
        ((), ProjectRootsState.MISSING),
        (
            (RootFacts(id="gone", path="/mnt/gone", readable=False, writable=False),),
            ProjectRootsState.UNREADABLE,
        ),
        (
            (
                RootFacts(id="ro", path="/mnt/ro", readable=True, writable=False),
                RootFacts(id="gone", path="/mnt/gone", readable=False, writable=False),
            ),
            ProjectRootsState.UNREADABLE,
        ),
        (
            (RootFacts(id="ro", path="/mnt/ro", readable=True, writable=False),),
            ProjectRootsState.UNUSABLE,
        ),
        (
            (
                RootFacts(id="ro", path="/mnt/ro", readable=True, writable=False),
                RootFacts(id="home", path="/home/you/projects", readable=True, writable=True),
            ),
            ProjectRootsState.OK,
        ),
    ],
)
def test_project_roots_are_classified_once_for_every_surface(roots, expected):
    """A vanished root outranks a read-only one; one usable root is enough."""
    assessment = assess_project_roots(roots)

    assert assessment.state is expected
    assert assessment.ok is (expected is ProjectRootsState.OK)
    assert (assessment.remediation is None) is assessment.ok


def test_every_project_roots_state_maps_to_a_doctor_severity():
    """Adding a state without deciding how `aq doctor` reports it is a bug."""
    from src.doctor.project_checks import _SEVERITY

    assert set(_SEVERITY) == set(ProjectRootsState)


def test_the_installer_and_the_doctor_read_the_same_verdict():
    """The reconciliation this module exists for, asserted directly.

    `src.install.wizard._project_root_check` and the `projects.roots` check
    both call `assess_project_roots`, so "installer says needs-attention,
    doctor says fine" cannot come back for any configuration.
    """
    from src.doctor.project_checks import _SEVERITY

    for roots in (
        (),
        (RootFacts(id="ro", path="/mnt/ro", readable=True, writable=False),),
        (RootFacts(id="gone", path="/mnt/gone", readable=False, writable=False),),
        (RootFacts(id="home", path="/home/you/projects", readable=True, writable=True),),
    ):
        assessment = assess_project_roots(roots)
        installer_ready = assessment.ok
        doctor_healthy = _SEVERITY[assessment.state] is Severity.OK

        assert installer_ready is doctor_healthy, assessment.detail


def test_project_roots_doctor_check_is_registered():
    assert default_registry().get("projects.roots") is not None
