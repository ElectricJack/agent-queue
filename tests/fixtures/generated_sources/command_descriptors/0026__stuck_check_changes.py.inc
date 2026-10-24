def _stuck_check_changes():
    return json.loads((ROOT / "tests/fixtures/contracts/stuck-check-contract-changes.json").read_text())


