

def _claim_abort_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/claim-abort-contract-changes.json").read_text()
    )


def _before_claim_abort_catalogue(catalogue=None):
    evidence = _claim_abort_changes()
    if catalogue is None:
        catalogue = _before_store_maintenance_catalogue()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["explicit_entry"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


def test_claim_abort_catalogue_only_adds_fenced_supervisor_control():
    evidence = _claim_abort_changes()
    assert evidence["command"] == "task_claim_abort"
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    catalogue = _before_retained_holder_repair_catalogue(
        _before_publication_hold_catalogue(_before_store_maintenance_catalogue())
    )
    assert hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_claim_abort_catalogue(catalogue), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
