def test_opaque_arguments_forward_additional_properties_without_wrapping():
    descriptor = _snapshot(handlers=discover_handler_inventory(NewHandler)).commands["newly_discovered"]
    raw = {"root": 3, "unknown": {"nested": [True, None, 2.5]}}
    assert argument_model(descriptor).model_validate(raw).model_dump() == raw


