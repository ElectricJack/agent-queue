def _wait_source_redispatch_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/wait-source-redispatch-contract-changes.json").read_text()
    )


def _before_wait_source_redispatch_catalogue():
    catalogue = _before_batch_authority_catalogue()
    evidence = _wait_source_redispatch_changes()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["explicit_entry"]
    catalogue["explicit"].remove(definition)
    assert catalogue["tool_categories"].pop(evidence["command"]) == evidence["tool_category"]
    return catalogue


def test_wait_source_redispatch_only_adds_scoped_recovery_command():
    evidence = _wait_source_redispatch_changes()
    assert evidence["command"] == "wait_source_redispatch"
    assert _wait_source_recovery_changes()["previous_fingerprint"] == (
        evidence["contract_fingerprint"]
    )
    catalogue = _before_task_list_limit_catalogue(_before_unread_inbox_catalogue())
    rendered = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(rendered.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
    previous = json.dumps(
        _before_task_list_limit_catalogue(
            _before_unread_inbox_catalogue(_before_wait_source_redispatch_catalogue())
        ),
        indent=2,
    ) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
