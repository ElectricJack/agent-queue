

def _wait_source_recovery_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/wait-source-recovery-contract-changes.json").read_text()
    )


def _before_wait_source_recovery_catalogue(catalogue=None):
    if catalogue is None:
        catalogue = _before_publication_hold_catalogue(
            _before_retained_holder_repair_catalogue(
                _before_claim_abort_catalogue(
                    _before_store_maintenance_catalogue(json.loads(render_command_catalogue()))
                )
            )
        )
    evidence = _wait_source_recovery_changes()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["definition"]
    definition.clear()
    definition.update(evidence["previous_definition"])
    return catalogue


def test_wait_source_recovery_only_extends_existing_scoped_command():
    evidence = _wait_source_recovery_changes()
    assert evidence["command"] == "wait_source_redispatch"
    contract = CONTRACTS.require(evidence["command"]).contract
    assert contract.fingerprint() == evidence["fingerprint"] != evidence["previous_fingerprint"]
    assert canonical_execution_document(contract.execution) == evidence["execution"]
    schema = evidence["definition"]["input_schema"]
    assert schema["properties"]["action"]["enum"] == ["redispatch", "blockers", "retire"]
    assert schema["properties"]["action"]["default"] == "redispatch"
    assert schema["required"] == ["project_id"]
    assert set(schema["properties"]) - set(evidence["previous_definition"]["input_schema"][
        "properties"]) == {"action", "limit"}
    catalogue = _before_unread_inbox_catalogue(
        _before_batch_authority_catalogue(_before_publication_hold_catalogue(
            _before_retained_holder_repair_catalogue(_before_claim_abort_catalogue(
                _before_store_maintenance_catalogue(json.loads(render_command_catalogue()))
            ))
        ))
    )
    assert hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"])
    previous = json.dumps(_before_wait_source_recovery_catalogue(catalogue), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
