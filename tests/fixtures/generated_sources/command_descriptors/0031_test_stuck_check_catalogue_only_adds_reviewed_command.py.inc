def test_stuck_check_catalogue_only_adds_reviewed_command():
    evidence = _stuck_check_changes()
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    previous = json.dumps(_before_stuck_check_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


