def _observation_changes():
    changes = json.loads(C2_CHANGES.read_text())
    assert set(changes["contracts"]) == {"list_projects", "list_tasks", "get_stuck_tasks"}
    return changes


