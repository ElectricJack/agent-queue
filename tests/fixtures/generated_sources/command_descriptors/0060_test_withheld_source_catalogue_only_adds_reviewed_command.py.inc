def test_withheld_source_catalogue_only_adds_reviewed_command():
    evidence = json.loads(WITHHELD_CHANGES.read_text())
    assert evidence["source_base"] == "35d58eee0183bcc25252931ae5958072983606d6"
    name = "integration_release_withheld_source"
    catalogue = _before_hibernation_catalogue()
    for later in EPIC_COMMANDS:
        catalogue["fallback"].pop(later)
    assert catalogue["fallback"].pop(name) == evidence["transport_definition"]
    assert CONTRACTS.require(name).contract.fingerprint() == evidence["contract_fingerprint"]
    # Removing exactly the new command must recover the prior bytes, including
    # schema field ordering. Existing commands and category maps stay pinned.
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == _dev_changes()["cli_catalogue_sha256"]


