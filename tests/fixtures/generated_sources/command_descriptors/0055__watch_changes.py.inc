def _watch_changes():
    changes = json.loads(WATCH_CHANGES.read_text())
    assert set(changes["playbook_fixtures"]) == {
        "tests/fixtures/playbooks/v2/stuck-task-watch/artifact.json",
        "tests/fixtures/playbooks/v2/stuck-task-watch/diagnostics.json",
    }
    reminder = "tests/fixtures/playbooks/v2/stuck-task-watch-reminder/"
    assert set(changes["new_playbook_fixtures"]) == {
        reminder + name for name in (
            "artifact.json", "artifact.sha256", "diagnostics.json", "manifest.md", "source.md",
        )
    }
    return changes


