def test_snapshot_and_nested_schemas_cannot_be_mutated_by_consumers():
    schema = {"type": "object", "properties": {"key": {"enum": ["one", "two"]}}}
    descriptor = _snapshot(
        handlers=[HandlerCommand("read", "Read", True)], fallback={"read": schema},
    ).commands["read"]
    schema["properties"].clear()
    descriptor.input_schema()["properties"].clear()
    descriptor.tool_definition()["input_schema"].clear()
    assert descriptor.input_schema()["properties"]["key"]["enum"] == ["one", "two"]
    with pytest.raises(FrozenInstanceError):
        descriptor.available = False
    with pytest.raises(TypeError):
        command_snapshot().commands["invented"] = descriptor


