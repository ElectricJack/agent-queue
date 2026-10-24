def _before_pool_delete_catalogue(catalogue=None):
    evidence = _pool_delete_changes()
    catalogue = _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue
    definitions = catalogue["explicit"]
    index = next(i for i, row in enumerate(definitions) if row["name"] == evidence["command"])
    assert definitions.pop(index) == evidence["transport_definition"]
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["category"]
    return catalogue


