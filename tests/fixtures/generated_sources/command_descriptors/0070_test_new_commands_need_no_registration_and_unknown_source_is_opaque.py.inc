def test_new_commands_need_no_registration_and_unknown_source_is_opaque():
    snapshot = _snapshot(handlers=discover_handler_inventory(NewHandler))
    registry = ContractRegistry()
    registry.replace_descriptors(snapshot)
    assert registry.names() == {"newly_discovered", "argumentless"}
    opaque = snapshot.commands["newly_discovered"]
    assert opaque.schema_quality == "opaque"
    assert opaque.input_schema()["additionalProperties"] is True
    assert opaque.effect_class == "destructive" and opaque.sensitivity == "protected"
    assert not opaque.retry_safe and not opaque.preview_available
    assert opaque.idempotency_mechanism == "none"
    assert snapshot.commands["argumentless"].input_schema()["additionalProperties"] is False
    assert argument_usage(len) is None
    native = _snapshot(handlers=[HandlerCommand("native", "Native", None)]).commands["native"]
    assert native.schema_source == "opaque"


