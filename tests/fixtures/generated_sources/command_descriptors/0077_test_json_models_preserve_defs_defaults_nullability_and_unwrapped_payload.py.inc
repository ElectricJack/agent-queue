def test_json_models_preserve_defs_defaults_nullability_and_unwrapped_payload(json_args):
    assert json_args.model_json_schema() == SCHEMA
    raw = {"count": 2, "choice": "yes", "nullable": None, "nested": {"flag": False}}
    assert json_args.model_validate(raw).model_dump() == raw
    minimal = {"count": 2, "choice": "no"}
    assert json_args.model_validate(minimal).model_dump() == minimal
    payload = {"root": {"payload": [1, None, False]}, "success": True}
    assert JsonObjectResult.model_validate(payload).model_dump() == payload


