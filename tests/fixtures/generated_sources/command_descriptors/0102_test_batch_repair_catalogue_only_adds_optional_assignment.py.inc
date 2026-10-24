def test_batch_repair_catalogue_only_adds_optional_assignment():
    _publication_catalogue()
    schema = command_snapshot().commands["task_set"].tool_definition()["input_schema"]
    assert schema["properties"]["repair_batch"]["type"] == "string"
    assert "repair_batch" not in schema.get("required", ())


