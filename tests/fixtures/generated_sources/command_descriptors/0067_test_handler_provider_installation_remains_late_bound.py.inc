def test_handler_provider_installation_remains_late_bound():
    calls = []

    def provider():
        calls.append(True)
        return SimpleNamespace()

    set_handler_provider(provider)
    try:
        assert calls == []
        first = command_snapshot()
        assert calls
        assert command_snapshot() is first
    finally:
        set_handler_provider(None)


