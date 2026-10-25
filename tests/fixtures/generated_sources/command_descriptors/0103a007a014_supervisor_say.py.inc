def _supervisor_say_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/supervisor-say-contract-changes.json").read_text()
    )


def _before_supervisor_say_catalogue():
    catalogue = json.loads(render_command_catalogue())
    evidence = _supervisor_say_changes()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["explicit_entry"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


def test_supervisor_say_catalogue_only_adds_initiated_conversation_post():
    evidence = _supervisor_say_changes()
    assert evidence["command"] == "supervisor_inbox_say"
    assert CONTRACTS.require(evidence["command"]).contract.fingerprint() == (
        evidence["contract_fingerprint"]
    )
    assert hashlib.sha256(render_command_catalogue().encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_supervisor_say_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]


