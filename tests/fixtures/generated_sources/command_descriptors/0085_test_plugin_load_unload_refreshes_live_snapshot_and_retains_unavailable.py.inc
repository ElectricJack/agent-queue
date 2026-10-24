async def test_plugin_load_unload_refreshes_live_snapshot_and_retains_unavailable(tmp_path, monkeypatch):
    class ProbePlugin(InternalPlugin):
        async def initialize(self, ctx):
            ctx.register_command("probe", self.probe)
            ctx.register_tool({"name": "probe", "description": "Probe", "input_schema": SCHEMA})

        async def probe(self, args):
            return {"success": True}

        async def shutdown(self, ctx):
            pass

    monkeypatch.setattr("src.plugins.internal.discover_internal_plugins",
                        lambda: [("src.plugins.internal.probe", ProbePlugin)])
    config = AppConfig(data_dir=str(tmp_path))
    plugins = PluginRegistry(db=AsyncMock(), bus=AsyncMock(), config=config)
    handler = CommandHandler(SimpleNamespace(plugin_registry=plugins), config)
    set_handler_provider(lambda: handler)
    try:
        before = command_snapshot()
        assert "probe" not in before.commands
        assert await plugins.load_internal_plugins() == 1
        loaded = command_snapshot()
        assert loaded is handler.command_descriptors
        assert loaded is not before and loaded.plugin_generation > before.plugin_generation
        assert loaded.commands["probe"].available and loaded.commands["probe"].plugin == "aq-probe"
        assert {"probe", "aq-probe.probe"} <= CONTRACTS.names()
        assert CONTRACTS.require("probe").contract.execution.args_model.model_json_schema() == SCHEMA
        await plugins.unload_plugin("aq-probe")
        unloaded = command_snapshot()
        assert unloaded.plugin_generation > loaded.plugin_generation
        assert not unloaded.commands["probe"].available and loaded.commands["probe"].available
        registration = CONTRACTS.require("probe")
        args = registration.contract.execution.args_model.model_validate({"count": 1, "choice": "yes"})
        assert await registration.invoke(args, None) == {
            "success": False, "error_code": "command_unavailable",
        }
    finally:
        set_handler_provider(None)


