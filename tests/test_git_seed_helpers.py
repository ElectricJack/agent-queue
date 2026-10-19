"""Git fixtures build independent repositories without filesystem snapshots."""

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module_name,constructor_name",
    [
        ("test_sync_and_merge", "git_repo"),
        ("test_git_manager", "git_repo"),
        ("test_git_mutex", "git_repo"),
        ("test_force_with_lease", "git_repo"),
        ("test_git_manager_async", "clone"),
        ("test_git_events", "clone"),
        ("test_worktree_manager", "_create_base_repo"),
        ("test_integration_gitops", "git_history_template"),
        ("test_epic_pr_review_evidence", "review_git_template"),
        ("test_delivery_consumers", "Origin"),
    ],
)
def test_fresh_repositories_have_no_copy_or_automatic_maintenance(
    tmp_path, monkeypatch, module_name, constructor_name
):
    # Real constructors, including receive-pack, must override user Git config.
    config = tmp_path / "gitconfig"
    config.write_text(
        "[maintenance]\n\tauto = true\n[gc]\n\tauto = 1\n[receive]\n\tautogc = true\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))

    def no_copy(*args, **kwargs):
        pytest.fail("Git fixtures must initialize repositories, not copy shared seeds")

    monkeypatch.setattr(shutil, "copytree", no_copy)
    module = importlib.import_module(f"tests.{module_name}")
    fixture = getattr(module, constructor_name)
    constructor = getattr(fixture, "__wrapped__", fixture)
    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
        if constructor_name == "clone":
            bare = module.bare_repo.__wrapped__(root)
            constructor(root, bare)
        else:
            constructor(root)
        for head in root.rglob("HEAD"):
            git_dir = head.parent
            if not (git_dir / "config").is_file():
                continue
            subprocess.run(
                ["git", f"--git-dir={git_dir}", "fsck", "--full"],
                check=True,
                capture_output=True,
            )
            # A later fixture must not see a prior test's repository mutation.
            assert not (git_dir / "private-mutation").exists()
            (git_dir / "private-mutation").write_text("private")
            assert not (git_dir / "objects" / "info" / "alternates").exists()

    # Both working clones must point at their own origin, never another test's.
    for root in roots:
        for config_path in root.rglob(".git/config"):
            origin = subprocess.check_output(
                ["git", "-C", str(config_path.parent.parent), "remote", "get-url", "origin"],
                text=True,
            ).strip()
            assert Path(origin).is_relative_to(root)

    automatic_writers = [
        event["argv"]
        for line in trace.read_text().splitlines()
        if (event := json.loads(line)).get("event") == "child_start"
        and any(arg in {"maintenance", "gc"} for arg in event.get("argv", []))
    ]
    assert automatic_writers == []
