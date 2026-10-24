def test_script_log_contract_only_adds_stream_selection():
    old = json.loads(GOLDEN.read_text())["legacy_contracts"]["job_logs"]["execution"]
    old["args_schema"]["properties"]["stream"] = {
        "default": "stdout", "enum": ["stdout", "stderr"], "type": "string",
    }
    assert _script_changes()["contracts"]["job_logs"]["execution"] == old


