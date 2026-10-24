def test_restart_hold_catalogue_preserves_original_pr_evidence():
    # Recover the PR's supervisor-health base through dev's frozen input chain.
    catalogue = _revert_target_repair_catalogue(_dev_catalogue())
    evidence = _restart_hold_changes()
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    approved = evidence["transport_definitions"]["restart_task"]
    [definition] = [item for item in catalogue["explicit"] if item["name"] == "restart_task"]
    assert definition == approved["previous_definition"]
    definition.clear()
    definition.update(approved["definition"])
    original = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == evidence["cli_catalogue_sha256"]


