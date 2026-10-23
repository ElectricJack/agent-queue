def _before_updater_catalogue(catalogue=None):
    catalogue = json.loads(render_command_catalogue()) if catalogue is None else catalogue
    for name, approved in _updater_changes()["transport_definitions"].items():
        [definition] = [item for item in catalogue["explicit"] if item["name"] == name]
        assert definition["description"] == approved["description"]
        assert _digest(definition) == approved["sha256"]
        definition["description"] = approved["previous_description"]
        assert _digest(definition) == approved["previous_sha256"]
    return catalogue


