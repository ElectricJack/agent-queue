def _epic_changes():
    changes = json.loads(EPIC_CHANGES.read_text())
    assert changes["source_base"] == "f88abe7fd245da853bd328187f01beda41e02e41"
    assert set(changes["transport_definitions"]) == set(EPIC_COMMANDS)
    assert set(changes["contract_fingerprints"]) == {"integration_cutover_containment"}
    return changes


