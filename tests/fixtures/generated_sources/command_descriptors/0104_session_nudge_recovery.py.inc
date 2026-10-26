

def _session_nudge_recovery_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/session-nudge-recovery-contract-changes.json").read_text()
    )


def _before_session_nudge_recovery_catalogue(catalogue=None):
    """Undo only optional nudge recovery before checking frozen catalogue history."""
    if catalogue is None:
        catalogue = _before_wait_source_redispatch_catalogue()
    evidence = _session_nudge_recovery_changes()
    schema = catalogue["fallback"][evidence["command"]]["input_schema"]
    [(prop, definition)] = evidence["added_property"].items()
    assert schema["properties"].pop(prop) == definition
    assert prop not in schema.get("required", ())
    return catalogue


def test_session_nudge_catalogue_only_adds_optional_pending_recovery():
    evidence = _session_nudge_recovery_changes()
    assert evidence["command"] == "session_nudge"
    assert set(evidence["added_property"]) == {"submit_pending"}
    assert set(evidence["transport_definitions"]) == {"session_nudge"}
    # Each sibling recording predates the other optional transport addition.
    catalogue = _before_unread_inbox_catalogue(_before_task_list_limit_catalogue())
    original = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_session_nudge_recovery_catalogue(catalogue), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )
    assert evidence["previous_cli_catalogue_sha256"] == (
        _supervisor_say_changes()["cli_catalogue_sha256"]
    )
