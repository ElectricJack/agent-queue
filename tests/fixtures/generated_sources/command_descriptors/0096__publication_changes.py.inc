def _publication_changes():
    changes = json.loads(PUBLICATION_CHANGES.read_text())
    assert changes["source_base"] == "f9a7591766d69481b309e8733be795e62ab0e59e"
    assert set(changes["transport_definitions"]) == {"git_push", "push_branch"}
    assert set(changes["added_commands"]) == {
        "integration_delegate_publication", "integration_revoke_publication",
    }
    return changes


