def test_structure_catalogue_preserves_reviewed_delta():
    changes = _structure_changes()
    for name, approved in changes["contracts"].items():
        contract = CONTRACTS.require(name).contract
        assert contract.fingerprint() == approved["fingerprint"]
        assert canonical_execution_document(contract.execution) == approved["execution"]
    previous = _revert_catalogue(
        _revert_structure_catalogue(_before_restart_hold_catalogue()),
        _wait_requeue_changes(),
    )
    assert hashlib.sha256((json.dumps(previous, indent=2) + "\n").encode()).hexdigest() == (
        _pr_repair_changes()["cli_catalogue_sha256"]
    )


