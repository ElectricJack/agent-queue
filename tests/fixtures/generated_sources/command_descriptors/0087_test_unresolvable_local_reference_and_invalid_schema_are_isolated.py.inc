def test_unresolvable_local_reference_and_invalid_schema_are_isolated():
    for schema in [
        {"type": "object", "properties": {"value": {"$ref": "#/$defs/missing"}}},
        {"type": "object", "properties": {"value": {"type": "not-a-json-type"}}},
    ]:
        snapshot = _snapshot(handlers=[HandlerCommand("invalid", "Invalid", True)],
                             fallback={"invalid": schema})
        registry = ContractRegistry()
        registry.replace_descriptors(snapshot)
        model = registry.require("invalid").contract.execution.args_model
        assert model.model_json_schema() == schema
        with pytest.raises(ValidationError):
            model.model_validate({"value": 1})


