def _before_batch_fix_catalogue():
    evidence = json.loads(BATCH_FIX_CHANGES.read_text())
    catalogue = _before_merge_catalogue()
    name = "integration_batch_fix"
    definition = next(row for row in catalogue["explicit"] if row["name"] == name)
    assert definition == evidence["transport_definition"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(name) == evidence["tool_category"]
    return catalogue


