def test_dev_redrive_contract_only_adds_explicit_source_adoption():
    old = json.loads(GOLDEN.read_text())["legacy_contracts"]["integration_redrive_child"][
        "execution"
    ]
    old["args_schema"]["properties"]["adopt_source"] = {
        "default": False, "type": "boolean",
    }
    old["result_schema"]["properties"]["completion_id"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}], "default": None,
    }
    old["receipt_projection"].insert(old["receipt_projection"].index("collection"), "completion_id")
    assert _dev_changes()["contracts"]["integration_redrive_child"]["execution"] == old


