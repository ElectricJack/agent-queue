def _store_maintenance_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/store-maintenance-contract-changes.json").read_text()
    )


def _before_store_maintenance_catalogue(catalogue=None):
    if catalogue is None:
        catalogue = _before_wait_source_recovery_catalogue(_before_explain_batch_catalogue())
    evidence = _store_maintenance_changes()
    assert catalogue["fallback"].pop(evidence["command"]) == evidence["transport_definition"]
    return catalogue


def test_store_maintenance_catalogue_only_adds_scoped_control():
    evidence = _store_maintenance_changes()
    assert evidence["command"] == "integration_store_maintenance"
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    catalogue = _before_wait_source_recovery_catalogue(_before_explain_batch_catalogue())
    assert hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_store_maintenance_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )
