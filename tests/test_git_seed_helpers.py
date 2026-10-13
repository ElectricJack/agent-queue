"""Shared seed construction must finish without launching background Git writers."""

import importlib
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "module_name,fixture_name",
    [
        ("test_sync_and_merge", "_git_repo_seed"),
        ("test_git_manager", "_git_repo_seed"),
        ("test_git_mutex", "_git_repo_seed"),
        ("test_force_with_lease", "_git_repo_seed"),
        ("test_git_manager_async", "_clone_seed"),
        ("test_git_events", "_clone_seed"),
        ("test_worktree_manager", "_base_repo_seed"),
        ("test_integration_gitops", "git_history_template"),
        ("test_epic_pr_review_evidence", "review_git_template"),
        ("test_delivery_consumers", "origin_template"),
    ],
)
def test_seed_construction_has_no_automatic_maintenance(
    tmp_path, monkeypatch, module_name, fixture_name
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
    seeds = tmp_path / "seeds"
    seeds.mkdir()

    def mktemp(name):
        path = seeds / name
        path.mkdir()
        return path

    factory = SimpleNamespace(mktemp=mktemp)
    module = importlib.import_module(f"tests.{module_name}")
    constructor = getattr(module, fixture_name).__wrapped__
    if fixture_name == "_clone_seed":
        empty = module._empty_repo_seed.__wrapped__(factory)
        constructor(factory, empty)
    else:
        constructor(factory)

    automatic_writers = [
        event["argv"]
        for line in trace.read_text().splitlines()
        if (event := json.loads(line)).get("event") == "child_start"
        and any(arg in {"maintenance", "gc"} for arg in event.get("argv", []))
    ]
    assert automatic_writers == []

    # Copy the complete seeds, with no lock exclusions, and verify object graphs
    # and refs in each copy. A copy's writes must not change the shared seed.
    snapshot = {
        path.relative_to(seeds): path.read_bytes() for path in seeds.rglob("*") if path.is_file()
    }
    for number in range(2):
        copied = tmp_path / f"copy-{number}"
        shutil.copytree(seeds, copied)
        assert {
            path.relative_to(copied): path.read_bytes()
            for path in copied.rglob("*")
            if path.is_file()
        } == snapshot
        for head in copied.rglob("HEAD"):
            git_dir = head.parent
            if not (git_dir / "config").is_file():
                continue
            subprocess.run(
                ["git", f"--git-dir={git_dir}", "fsck", "--full"],
                check=True,
                capture_output=True,
            )
            (git_dir / "copy-only").write_text("private mutation")
    assert {
        path.relative_to(seeds): path.read_bytes() for path in seeds.rglob("*") if path.is_file()
    } == snapshot
