@pytest.mark.parametrize("source", ["explicit_tool", "fallback"])
def test_declared_schema_precedes_argument_usage(source):
    schema = {"type": "object", "properties": {"count": {"type": "integer", "default": 3}}}
    snapshot = _snapshot(
        handlers=[HandlerCommand("read", "Read", False)],
        explicit=[{"name": "read", "input_schema": schema}] if source == "explicit_tool" else [],
        fallback={"read": schema},
    )
    descriptor = snapshot.commands["read"]
    assert descriptor.input_schema() == schema and descriptor.schema_source == source


