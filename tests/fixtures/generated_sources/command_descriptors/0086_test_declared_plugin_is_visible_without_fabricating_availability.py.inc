def test_declared_plugin_is_visible_without_fabricating_availability():
    plugin = PluginCommand.from_definition({"name": "optional", "input_schema": SCHEMA},
                                           plugin="third-party", available=False)
    descriptor = _snapshot(plugins=[plugin]).commands["optional"]
    assert descriptor.origin == "plugin" and not descriptor.available
    assert descriptor.schema_source == "explicit_tool"


