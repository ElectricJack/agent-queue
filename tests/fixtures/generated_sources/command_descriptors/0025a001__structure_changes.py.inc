def _structure_changes():
    changes = json.loads(STRUCTURE_CHANGES.read_text())
    assert changes["source_base"] == "908112dce62681fb4761e80a218382c75fc939cf"
    assert changes["source_commit"] == "f136e4af2c8f05a3d52cce6118fab78e03733a9e"
    assert set(changes["added_commands"]) == {"task_split"}
    assert set(changes["transport_definitions"]) == {"reparent_task"}
    assert set(changes["contracts"]) == {"integration_mutate_hierarchy"}
    return changes


