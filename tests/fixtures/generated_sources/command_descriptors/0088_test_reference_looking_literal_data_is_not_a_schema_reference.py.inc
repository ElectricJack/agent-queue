def test_reference_looking_literal_data_is_not_a_schema_reference():
    literal = {"$ref": "https://example.invalid/literal-not-a-reference"}
    schema = {"type": "object", "properties": {"data": {"const": literal, "default": literal}}}
    descriptor = _snapshot(handlers=[HandlerCommand("literal", "Literal", True)],
                           fallback={"literal": schema}).commands["literal"]
    assert argument_model(descriptor).model_validate({"data": literal}).model_dump() == {"data": literal}


