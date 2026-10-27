def _retained_holder_repair_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/retained-holder-repair-contract-changes.json").read_text()
    )


def _before_retained_holder_repair_catalogue(catalogue=None):
    if catalogue is None:
        catalogue = _before_publication_hold_catalogue(json.loads(render_command_catalogue()))
    evidence = _retained_holder_repair_changes()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["explicit_entry"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


def test_retained_holder_repair_only_adds_evidence_fenced_recovery_command():
    evidence = _retained_holder_repair_changes()
    assert evidence["command"] == "recover_retained_holder"
    assert evidence["source_base"] == "8333fd66a4a58abe6396a2841df4a63a6e7532f4"
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    rendered = json.dumps(
        _before_publication_hold_catalogue(json.loads(render_command_catalogue())), indent=2
    ) + "\n"
    assert hashlib.sha256(rendered.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
    previous = json.dumps(_before_retained_holder_repair_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
