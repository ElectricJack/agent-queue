def _obsolete_child_changes():
    changes = json.loads(OBSOLETE_CHILD_CHANGES.read_text())
    assert changes["source_base"] == "feeb89e65e4bb5e2a4a2033fa32f967d1f66fce9"
    assert set(changes["contracts"]) == {"integration_redrive_child"}
    assert set(changes["transport_definitions"]) == {"integration_redrive_child"}
    return changes



