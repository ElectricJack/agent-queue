@pytest.mark.parametrize("payload", [{1: "value"}, {"value": (1, 2)},
                                      {"value": float("inf")}, []])
def test_result_model_rejects_non_json_values(payload):
    with pytest.raises(ValidationError):
        JsonObjectResult.model_validate(payload)


