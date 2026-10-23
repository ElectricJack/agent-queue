def _held_release_changes():
    changes = json.loads(HELD_RELEASE_CHANGES.read_text())
    assert changes["source_base"] == "2fcad876c53308a9d3b0e104258aeb2f4f50f6ce"
    assert set(changes["transport_definitions"]) == {"doctor"}
    assert set(changes["added_contracts"]) == {
        "spec_ingest_legacy_adopt", "task_batch_release", "task_batch_retire",
    }
    return changes


