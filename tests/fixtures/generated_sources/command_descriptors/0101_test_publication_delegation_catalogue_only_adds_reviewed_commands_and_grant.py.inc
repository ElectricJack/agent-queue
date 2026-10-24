def test_publication_delegation_catalogue_only_adds_reviewed_commands_and_grant():
    changes = _publication_changes()
    _withheld_catalogue()
    snapshot = command_snapshot()
    [(prop, schema)] = changes["added_property"].items()
    for name, approved in changes["transport_definitions"].items():
        definition = snapshot.commands[name].tool_definition()
        assert definition["input_schema"]["properties"][prop] == schema
        assert prop not in definition["input_schema"].get("required", ())
        assert CONTRACTS.require(name).contract.fingerprint() != approved["previous_fingerprint"]


