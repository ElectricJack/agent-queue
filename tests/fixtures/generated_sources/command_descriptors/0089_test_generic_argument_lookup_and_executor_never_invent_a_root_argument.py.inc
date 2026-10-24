def test_generic_argument_lookup_and_executor_never_invent_a_root_argument():
    from src.playbooks.executors.command import _build_args
    from src.playbooks.explanation import canonical_effect
    from src.playbooks.validation import RegistryContractLookup

    registry = ContractRegistry()
    registry.replace_descriptors(_snapshot(handlers=discover_handler_inventory(NewHandler)))
    registration = registry.require("newly_discovered")
    info = RegistryContractLookup(registry).get("newly_discovered")
    assert not info.arguments
    assert info.argument_schema["additionalProperties"] is True
    raw = {"root": {"nested": 5}, "another": 1}
    args, forwarded, error = _build_args(registration, SimpleNamespace(inputs=raw), key=None)
    assert error is None and forwarded == raw and args.model_dump() == raw
    assert canonical_effect(registration.contract).text == "Composite using a JSON object"


