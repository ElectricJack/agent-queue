def _dev_changes():
    changes = json.loads(DEV_CHANGES.read_text())
    assert changes["source_base"] == "b17f5d8adb9bc2c15ddeaef8260812f281ee055b"
    assert set(changes["contracts"]) == {"integration_redrive_child"}
    assert set(changes["transport_definitions"]) == {"integration_redrive_child"}
    return changes


