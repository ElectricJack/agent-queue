def _before_pr_repair_catalogue():
    evidence = _pr_repair_changes()
    catalogue = json.loads(render_command_catalogue())
    assert catalogue["fallback"].pop(evidence["command"]) == evidence["transport_definition"]
    return catalogue


