def test_leaf_catalogue_only_adds_reviewed_commands():
    evidence = _leaf_changes()
    assert evidence["source_base"] == "120f820ee85acd957525ac2b77c4bacaeed4c728"
    assert not evidence["contracts"] and not evidence["transport_definitions"]
    snapshot = command_snapshot()
    for name, approved in evidence["new_commands"].items():
        assert evidence["catalogue_sections"]["explicit"]["entries"][name]["previous"] is None
        assert _digest(snapshot.commands[name].tool_definition()) == (
            approved["transport_sha256"]), name
        assert CONTRACTS.require(name).contract.fingerprint() == (
            approved["contract_fingerprint"]), name
    # Removing exactly the leaf commands recovers the merged epic bytes.
    previous = json.dumps(_before_leaf_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence[
        "previous_cli_catalogue_sha256"] == _merge_changes()["cli_catalogue_sha256"]


