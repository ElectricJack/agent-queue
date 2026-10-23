def _before_restart_hold_catalogue():
    catalogue = json.loads(render_command_catalogue())
    approved = _restart_hold_changes()["transport_definitions"]["restart_task"]
    [definition] = [item for item in catalogue["explicit"] if item["name"] == "restart_task"]
    assert definition == approved["definition"]
    definition.clear()
    definition.update(approved["previous_definition"])
    return catalogue


