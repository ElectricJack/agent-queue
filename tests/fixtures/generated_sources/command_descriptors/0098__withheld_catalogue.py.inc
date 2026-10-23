def _withheld_catalogue():
    """The publication catalogue minus exactly the reviewed delegation delta."""
    changes = _publication_changes()
    catalogue = _publication_catalogue()
    for name, added in changes["added_commands"].items():
        assert catalogue["fallback"].pop(name) == added["transport_definition"]
        assert CONTRACTS.require(name).contract.fingerprint() == added["contract_fingerprint"]
    [(prop, schema)] = changes["added_property"].items()
    seen = set()
    for _plugin, definitions in catalogue["internal"]:
        for definition in definitions:
            if definition["name"] in changes["transport_definitions"]:
                assert definition["input_schema"]["properties"].pop(prop) == schema
                seen.add(definition["name"])
    assert seen == set(changes["transport_definitions"])
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == changes["previous_cli_catalogue_sha256"]
    return catalogue


