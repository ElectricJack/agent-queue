def test_remote_schema_references_cannot_fetch_resources(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("network access"))
    descriptor = _snapshot(
        handlers=[HandlerCommand("remote", "Remote", True)],
        fallback={"remote": {"type": "object", "properties": {
            "data": {"$ref": "https://example.invalid/schema"},
        }}},
    ).commands["remote"]
    model = argument_model(descriptor)
    with pytest.raises(ValidationError, match="local"):
        model.model_validate({"data": {}})


