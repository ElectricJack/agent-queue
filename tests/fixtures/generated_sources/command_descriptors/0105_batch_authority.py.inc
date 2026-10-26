

def _batch_authority_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/batch-authority-contract-changes.json").read_text()
    )


def _before_batch_authority_catalogue():
    evidence = _batch_authority_changes()
    catalogue = json.loads(render_command_catalogue())
    assert catalogue["fallback"].pop(evidence["command"]) == evidence["transport_definition"]
    return catalogue


def test_batch_authority_catalogue_only_adds_supervisor_recovery_control():
    evidence = _batch_authority_changes()
    assert evidence["source_base"] == "90fd25530010c373855fa415af08d9dc8291c6e0"
    assert evidence["command"] == "integration_rebind_batch_authority"
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    previous = json.dumps(_before_batch_authority_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    assert hashlib.sha256(render_command_catalogue().encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
