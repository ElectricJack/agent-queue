def test_updater_catalogue_only_changes_guarded_update_description():
    evidence = _updater_changes()
    assert set(evidence["transport_definitions"]) == {"update_and_restart"}
    catalogue = _before_wait_requeue_catalogue()
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    assert evidence["previous_cli_catalogue_sha256"] == _pr_repair_changes()["cli_catalogue_sha256"]
    for name, approved in evidence["transport_definitions"].items():
        [definition] = [item for item in catalogue["explicit"] if item["name"] == name]
        definition["description"] = approved["description"]
        assert _digest(definition) == approved["sha256"]
    original = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == evidence["cli_catalogue_sha256"]


