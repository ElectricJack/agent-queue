def _revert_supervisor_health_catalogue(catalogue):
    """Restore only the reviewed description text to recover the hibernation bytes."""
    catalogue = _revert_target_repair_catalogue(catalogue)
    changes = _supervisor_health_changes()
    for name, approved in changes["transport_definitions"].items():
        [definition] = [item for item in catalogue["explicit"] if item["name"] == name]
        assert definition["description"] == approved["description"]
        definition["description"] = approved["previous_description"]
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == changes["previous_cli_catalogue_sha256"]
    return catalogue


