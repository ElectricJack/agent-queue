def _publication_catalogue():
    """Remove only the optional batch assignment to recover prior catalogue bytes."""
    changes = _batch_repair_changes()
    catalogue = _revert_hibernation_catalogue(
        _revert_supervisor_health_catalogue(_dev_catalogue())
    )
    [(prop, schema)] = changes["added_property"].items()
    [definition] = [item for item in catalogue["explicit"] if item["name"] == "task_set"]
    assert definition["input_schema"]["properties"].pop(prop) == schema
    assert prop not in definition["input_schema"].get("required", ())
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == changes["previous_cli_catalogue_sha256"]
    return catalogue


