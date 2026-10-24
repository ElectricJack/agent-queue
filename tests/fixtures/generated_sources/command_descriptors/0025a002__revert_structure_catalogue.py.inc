def _revert_structure_catalogue(catalogue):
    changes = _structure_changes()
    for name, approved in changes["added_commands"].items():
        assert catalogue["fallback"].pop(name) == approved["transport_definition"]
        assert catalogue["tool_categories"].pop(name) == approved["category"]
        assert CONTRACTS.require(name).contract.fingerprint() == approved["contract_fingerprint"]
    for name, approved in changes["transport_definitions"].items():
        [definition] = [row for row in catalogue["explicit"] if row["name"] == name]
        assert definition == approved["current"]
        schema = json.loads(json.dumps(definition["input_schema"]))
        for field in ("expected_old_generation", "expected_new_generation"):
            assert schema["properties"].pop(field)["type"] == "integer"
            assert field not in schema.get("required", ())
        assert schema == approved["previous"]["input_schema"]
        definition.clear()
        definition.update(approved["previous"])
    return catalogue


