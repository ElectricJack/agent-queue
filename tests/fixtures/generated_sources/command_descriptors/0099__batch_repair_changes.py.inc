def _batch_repair_changes():
    changes = json.loads(BATCH_REPAIR_CHANGES.read_text())
    assert changes["source_base"] == "ee36726bc1ae824a79787c2805c998422197da2a"
    assert changes["source_commit"] == "67575c6b9a43f820d824ff2bdb7d6be41ec21db3"
    assert set(changes["transport_definitions"]) == {"task_set"}
    assert set(changes["added_property"]) == {"repair_batch"}
    return changes


