@pytest.mark.parametrize("payload", [
    {"count": "2", "choice": "yes"}, {"count": True, "choice": "yes"},
    {"count": 0, "choice": "yes"}, {"count": 2, "choice": "maybe"},
    {"count": 2}, {"count": 2, "choice": "yes", "extra": 1},
    {"count": 2, "choice": "yes", "nested": {"flag": "false"}},
    {"count": 2, "choice": "yes", "nullable": 1},
    {"count": float("nan"), "choice": "yes"},
])
def test_strict_schema_validation_precedes_coercion(json_args, payload):
    with pytest.raises(ValidationError):
        json_args.model_validate(payload)


