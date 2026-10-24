def _before_retention_catalogue(catalogue=None):
    evidence = _retention_changes()
    catalogue = _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue
    entries = [e for e in catalogue["explicit"] if e.get("name") == evidence["command"]]
    assert entries == [evidence["explicit_entry"]]
    catalogue["explicit"].remove(entries[0])
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


