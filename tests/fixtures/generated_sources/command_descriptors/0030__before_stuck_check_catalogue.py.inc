def _before_stuck_check_catalogue():
    evidence = _stuck_check_changes()
    catalogue = _before_pr_repair_catalogue()
    assert catalogue["fallback"].pop(evidence["command"]) == evidence["transport_definition"]
    return catalogue


