def test_typed_playbook_schema_wins_and_explicit_transport_discrepancy_is_audited():
    contract = CONTRACTS.require("message_send").contract
    schema = {"type": "object", "properties": {"legacy": {"type": "string"}}}
    snapshot = _snapshot(
        handlers=[HandlerCommand("message_send", "Send", True)],
        explicit=[{"name": "message_send", "description": "Legacy", "input_schema": schema}],
        fallback={"message_send": {"type": "object", "additionalProperties": False}},
        typed={"message_send": contract},
    )
    descriptor = snapshot.commands["message_send"]
    assert descriptor.input_schema() == contract.execution.args_model.model_json_schema()
    assert descriptor.input_schema("transport") == schema
    assert descriptor.schema_source == "typed_contract"
    assert descriptor.transport_schema_source == "explicit_tool"
    assert descriptor.audit == ("explicit_transport_schema_differs_from_typed_playbook_schema",)
    assert descriptor.result_mode == "typed"
    assert descriptor.idempotency_mechanism == contract.execution.idempotency.mode
    with pytest.raises(ValueError, match="schema view"):
        descriptor.input_schema("unknown")


