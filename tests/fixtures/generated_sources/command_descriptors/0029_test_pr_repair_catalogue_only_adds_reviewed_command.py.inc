def test_pr_repair_catalogue_only_adds_reviewed_command():
    evidence = _pr_repair_changes()
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    previous = json.dumps(_before_pr_repair_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


