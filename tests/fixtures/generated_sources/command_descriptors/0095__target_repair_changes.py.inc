def _target_repair_changes():
    changes = json.loads(TARGET_REPAIR_CHANGES.read_text())
    assert changes["source_commit"] == "8ec0c84e3afdd0b9bc79b9dbd7b59df197a47ff6"
    assert set(changes["added_commands"]) == {"integration_repair_target"}
    return changes


