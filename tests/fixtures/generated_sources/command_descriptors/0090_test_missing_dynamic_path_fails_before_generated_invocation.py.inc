def test_missing_dynamic_path_fails_before_generated_invocation(json_args):
    from src.playbooks.expressions import (
        BindingRef,
        ResolutionScope,
        ValueResolutionError,
        resolve_value,
    )

    # The expression layer's missing marker cannot be coerced into JSON or
    # silently supplied as a default by the generated argument model.
    with pytest.raises(ValueResolutionError):
        value = resolve_value(BindingRef(binding="result", path="missing"),
                              ResolutionScope(bindings={"result": {"known": 1}}))
        json_args.model_validate({"count": value, "choice": "yes"})


