def _revert_target_repair_catalogue(catalogue):
    """Remove the new repair command and recover the preceding catalogue bytes."""
    repair = _target_repair_changes()
    for name, approved in repair["added_commands"].items():
        assert catalogue["fallback"].pop(name) == approved["transport_definition"]
        assert CONTRACTS.require(name).contract.fingerprint() == approved["contract_fingerprint"]
    assert hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest() == (
        repair["previous_cli_catalogue_sha256"]
    )
    return catalogue


