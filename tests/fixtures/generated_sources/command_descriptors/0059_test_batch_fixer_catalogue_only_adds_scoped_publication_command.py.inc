def test_batch_fixer_catalogue_only_adds_scoped_publication_command():
    evidence = json.loads(BATCH_FIX_CHANGES.read_text())
    assert evidence["source_base"] == "d496e8a12c3e863aee09910856b018809f3eb21c"
    name = "integration_batch_fix"
    assert CONTRACTS.require(name).contract.fingerprint() == evidence["contract_fingerprint"]
    previous = json.dumps(_before_batch_fix_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence[
        "previous_cli_catalogue_sha256"
    ]


