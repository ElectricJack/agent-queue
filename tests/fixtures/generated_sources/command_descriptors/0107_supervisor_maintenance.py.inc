

def _supervisor_maintenance_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/supervisor-maintenance-contract-changes.json").read_text()
    )


def _before_supervisor_maintenance_catalogue():
    evidence = _supervisor_maintenance_changes()
    catalogue = json.loads(render_command_catalogue())
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["explicit_entry"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


def test_supervisor_maintenance_only_adds_authenticated_audit_command():
    evidence = _supervisor_maintenance_changes()
    assert evidence["source_base"] == "9a9b8e8ef992d1ff699755a9383ed3c91e2844ea"
    assert evidence["command"] == "maintenance_preflight"
    assert evidence["explicit_entry"]["input_schema"]["properties"]["action"]["enum"] == [
        "update", "update.check", "db.backup",
    ]
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    rendered = render_command_catalogue()
    assert hashlib.sha256(rendered.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
    previous = json.dumps(_before_supervisor_maintenance_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
