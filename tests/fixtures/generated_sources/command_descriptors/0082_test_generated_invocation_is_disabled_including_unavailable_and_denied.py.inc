async def test_generated_invocation_is_disabled_including_unavailable_and_denied():
    descriptor = _snapshot(handlers=discover_handler_inventory(NewHandler)).commands["newly_discovered"]
    registration = generated_registration(descriptor)
    args = registration.contract.execution.args_model.model_validate({})
    with pytest.raises(GeneratedInvocationDisabled):
        await registration.invoke(args, None)
    for current, code in [(replace(descriptor, available=False), "command_unavailable"),
                          (replace(descriptor, denial_reason="forbidden"), "playbook_command_denied")]:
        registration = generated_registration(current)
        args = registration.contract.execution.args_model.model_validate({})
        assert (await registration.invoke(args, None)) == {"success": False, "error_code": code}


