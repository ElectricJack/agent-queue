def test_workspace_catalogue_only_changes_guarded_ownership_controls():
    evidence = _workspace_changes()
    snapshot = command_snapshot()
    assert set(evidence["transport_definitions"]) == {"release_workspace"}
    assert set(evidence["new_commands"]) == {"rebind_workspace"}
    for name, approved in evidence["new_commands"].items():
        assert _digest(snapshot.commands[name].tool_definition()) == approved["transport_sha256"]
        assert CONTRACTS.require(name).contract.fingerprint() == approved["contract_fingerprint"]
    previous = _before_restart_hold_catalogue()
    assert hashlib.sha256((json.dumps(previous, indent=2) + "\n").encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


