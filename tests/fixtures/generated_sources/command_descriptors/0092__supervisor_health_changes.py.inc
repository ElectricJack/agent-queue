def _supervisor_health_changes():
    changes = json.loads(SUPERVISOR_HEALTH_CHANGES.read_text())
    assert changes["source_base"] == "a73e55ae16605561e6ce371bab9685455cc1f34e"
    assert set(changes["transport_definitions"]) == {"supervisor_restart"}
    return changes


