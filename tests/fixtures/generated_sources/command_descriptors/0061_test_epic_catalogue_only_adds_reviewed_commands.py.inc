def test_epic_catalogue_only_adds_reviewed_commands():
    evidence = _epic_changes()
    catalogue = _before_hibernation_catalogue()
    for name in EPIC_COMMANDS:
        assert catalogue["fallback"].pop(name) == evidence["transport_definitions"][name], name
    for name, fingerprint in evidence["contract_fingerprints"].items():
        assert CONTRACTS.require(name).contract.fingerprint() == fingerprint, name
    # Removing exactly the epic's commands recovers the withheld-source bytes.
    previous = json.dumps(catalogue, indent=2) + "\n"
    withheld = json.loads(WITHHELD_CHANGES.read_text())
    assert hashlib.sha256(previous.encode()).hexdigest() == withheld["cli_catalogue_sha256"]


