def _unread_inbox_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/unread-inbox-contract-changes.json").read_text()
    )


def _before_unread_inbox_catalogue(catalogue=None):
    if catalogue is None:
        catalogue = json.loads(render_command_catalogue())
    approved = _unread_inbox_changes()["transport_definitions"]["message_inbox"]
    [definition] = [row for row in catalogue["explicit"] if row["name"] == "message_inbox"]
    assert definition == approved["current"]
    definition.clear()
    definition.update(approved["previous"])
    return catalogue


def test_unread_inbox_only_changes_transport_description():
    evidence = _unread_inbox_changes()
    assert set(evidence["transport_definitions"]) == {"message_inbox"}
    approved = evidence["transport_definitions"]["message_inbox"]
    assert {**approved["previous"], "description": approved["current"]["description"]} == (
        approved["current"]
    )
    assert _digest(approved["previous"]) == approved["previous_sha256"]
    assert _digest(approved["current"]) == approved["sha256"]
    catalogue = _before_task_list_limit_catalogue()
    rendered = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_unread_inbox_catalogue(catalogue), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
