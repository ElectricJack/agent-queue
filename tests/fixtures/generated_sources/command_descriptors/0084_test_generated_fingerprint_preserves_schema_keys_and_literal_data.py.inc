def test_generated_fingerprint_preserves_schema_keys_and_literal_data():
    def contract(value):
        schema = {"type": "object", "properties": {
            "description": {"const": {"title": value, "required": [2, "one"]}},
        }}
        descriptor = _snapshot(handlers=[HandlerCommand("literal", "Literal", True)],
                               fallback={"literal": schema}).commands["literal"]
        return generated_registration(descriptor).contract, schema

    first, schema = contract("first")
    second, _ = contract("second")
    assert canonical_execution_document(first.execution)["args_schema"] == schema
    assert first.fingerprint() != second.fingerprint()


