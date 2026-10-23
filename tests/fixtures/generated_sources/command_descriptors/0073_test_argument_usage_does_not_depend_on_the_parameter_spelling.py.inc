def test_argument_usage_does_not_depend_on_the_parameter_spelling():
    async def renamed(self, payload):
        return payload

    assert argument_usage(renamed) is True


