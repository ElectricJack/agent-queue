def _script_changes():
    changes = json.loads(SCRIPT_CHANGES.read_text())
    assert set(changes["contracts"]) == {"job_logs"}
    assert set(changes["playbook_fixtures"]) == {
        "tests/fixtures/playbooks/v2/default-assignment-routing/artifact.json",
    }
    return changes


