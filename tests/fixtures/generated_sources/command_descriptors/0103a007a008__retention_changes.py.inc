def _retention_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/operational-retention-contract-changes.json").read_text()
    )


